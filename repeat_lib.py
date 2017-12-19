import json
import os
import sys
import signal
import traceback
import time
import logging

import socket
import select
import threading
import portability
# Portability between 2 & 3
if portability.is_py2:
    import Queue as queue
else:
    import queue as queue

import specifications
import tasks
import shared_memory_request
import keyboard_request
import mouse_request
import tool_request
import system_host_request
import system_client_request

class RepeatClient(object):
    """Server encoding"""
    REPEAT_SERVER_ENCODING = 'UTF-8'

    """Server will terminate connection if not received anything after this period of time"""
    REPEAT_SERVER_TIMEOUT_SEC = 10

    """Delimiter between messages (Receiver must receive at least one delimiter between two messages. However, two or more is also acceptable)"""
    MESSAGE_DELIMITER = '\x02'

    """Client must send keep alive message to maintain the connection with server.
    Therefore the client timeout has to be less than server timeout"""
    REPEAT_CLIENT_TIMEOUT_SEC = REPEAT_SERVER_TIMEOUT_SEC * 0.3

    def __init__(self, host = 'localhost', port = 9999):
        super(RepeatClient, self).__init__()
        self.host = host
        self.port = port
        self.socket = None
        self.is_terminated = False

        self.synchronization_objects = {}
        self.send_queue = queue.Queue()
        self.task_manager = tasks.TaskManager(self)

        self.system = system_host_request.SystemHostRequest(self)
        self.system_client = system_client_request.SystemClientRequest(self)

        self.shared_memory = shared_memory_request.SharedMemoryRequest(self)
        self.mouse = mouse_request.MouseRequest(self)
        self.key = keyboard_request.KeyboardRequest(self)
        self.tool = tool_request.ToolRequest(self)

        self._previous_message = []

    def _clear_queue(self):
        while not self.send_queue.empty():
            self.send_queue.get()

    def start(self):
        self._clear_queue()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))

        self.system_client.identify()
        logger.info("Successfully started python client")

    def stop(self):
        self._clear_queue()
        self.socket.close()

    def process_write(self):
        while not self.is_terminated:
            data = None
            try:
                data = self.send_queue.get(block = True, timeout = RepeatClient.REPEAT_CLIENT_TIMEOUT_SEC)
            except queue.Empty as e:
                pass

            keep_alive = data is None
            if keep_alive:
                self.system.keep_alive()
            else:
                to_send = '%s%s%s%s%s' % (RepeatClient.MESSAGE_DELIMITER, RepeatClient.MESSAGE_DELIMITER, \
                                            json.dumps(data), RepeatClient.MESSAGE_DELIMITER, RepeatClient.MESSAGE_DELIMITER)

                if portability.is_py2:
                    self.socket.sendall(to_send)
                else:
                    self.socket.sendall(to_send.encode(RepeatClient.REPEAT_SERVER_ENCODING))

        logger.info("Write process terminated...")

    def _extract_messages(self, received_data):
        output = []
        for char in received_data:
            if char == RepeatClient.MESSAGE_DELIMITER:
                if len(self._previous_message) > 0:
                    output.append(''.join(self._previous_message))
                    del self._previous_message[:]
            else:
                self._previous_message.append(char)

        return output

    def process_read(self):
        while not self.is_terminated:
            data = None
            try:
                ready = select.select([self.socket], [], [], RepeatClient.REPEAT_CLIENT_TIMEOUT_SEC)
                if ready[0]:
                    data = self.socket.recv(1024).decode(RepeatClient.REPEAT_SERVER_ENCODING)
                else:
                    data = None
            except socket.error as se:
                logger.critical(traceback.format_exc())
                break
            except Exception as e:
                logger.critical(traceback.format_exc())

            if data is None or len(data.strip()) == 0:
                continue

            messages = self._extract_messages(data)
            for message in messages:
                try:
                    parsed = json.loads(message)
                    message_type = parsed['type']
                    message_id = parsed['id']
                    message_content = parsed['content']

                    if message_id in self.synchronization_objects:
                        returned_object = parsed['content']['message']
                        cv = self.synchronization_objects.pop(message_id)

                        set_value = returned_object is None \
                                    or (hasattr(returned_object, '__len__') and len(returned_object) > 0) \
                                    or (not hasattr(returned_object, '__len__'))
                        # Give the output of this to the caller
                        if set_value:
                            self.synchronization_objects[message_id] = returned_object

                        cv.set()
                    else:
                        if message_type != 'task':
                            logger.warning("Unknown id %s. Drop message..." % message_id)
                            continue

                        def to_run():
                            processing_id = message_id
                            processing_content = message_content
                            processing_type = message_type

                            reply = None
                            try:
                                reply = self.task_manager.process_message(processing_id, processing_content)
                            except:
                                logger.warning(traceback.format_exc())

                            if reply is None:
                                return

                            self.send_queue.put({
                                    'type' : processing_type,
                                    'id' : processing_id,
                                    'content' : reply
                                })

                        running = threading.Thread(target=to_run)
                        running.start()
                except Exception as e:
                    logger.warning(traceback.format_exc())

        logger.info("Read process terminated...")
##############################################################################################################################

if __name__ == "__main__":
    logging.basicConfig(format='[PYTHON][%(levelname)s][%(filename)s][%(lineno)d] - %(message)s', level=logging.DEBUG)
    logger = logging.getLogger(__name__)

    client = RepeatClient()

    client.start()

    write_thread = threading.Thread(target=client.process_write)
    read_thread = threading.Thread(target=client.process_read)

    def terminate_repeat_client(*args, **kwargs):
        client.is_terminated = True
        write_thread.join()
        read_thread.join()
        client.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, terminate_repeat_client)

    write_thread.start()
    read_thread.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        logger.info("Terminating repeat client...")
        terminate_repeat_client()
