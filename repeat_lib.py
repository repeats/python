import json
import os
import sys
import signal
import traceback
import time
import imp

import socket
import select
import threading
import Queue

import specifications
import shared_memory_request
import keyboard_request
import mouse_request
import tool_request
import system_host_request
import system_client_request


class RepeatClient(object):
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
        self.send_queue = Queue.Queue()
        self.task_manager = TaskManager(self)

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
        print "Successfully started python client"

    def stop(self):
        self._clear_queue()
        self.socket.close()

    def process_write(self):
        while not self.is_terminated:
            data = None
            try:
                data = self.send_queue.get(block = True, timeout = RepeatClient.REPEAT_CLIENT_TIMEOUT_SEC)
            except Queue.Empty as e:
                pass

            keep_alive = data is None
            if keep_alive:
                self.system.keep_alive()
            else:
                to_send = '%s%s%s%s%s' % (RepeatClient.MESSAGE_DELIMITER, RepeatClient.MESSAGE_DELIMITER, \
                                            json.dumps(data), RepeatClient.MESSAGE_DELIMITER, RepeatClient.MESSAGE_DELIMITER)
                self.socket.sendall(to_send)

        print "Write process terminated..."

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
                    data = self.socket.recv(1024)
                else:
                    data = None
            except socket.error as se:
                print traceback.format_exc()
                break
            except Exception as e:
                print traceback.format_exc()

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

                        if returned_object is None or len(returned_object) > 0: #Give the output of this to the caller
                            self.synchronization_objects[message_id] = returned_object

                        cv.set()
                    else:
                        if message_type != 'task':
                            print "Unknown id %s. Drop message..." % message_id
                            continue

                        def to_run():
                            processing_id = message_id
                            processing_content = message_content
                            processing_type = message_type

                            reply = self.task_manager.process_message(processing_id, processing_content)
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
                    print traceback.format_exc()

        print "Read process terminated..."


##############################################################################################################################
def generate_reply(status, message):
    return {
        'status' : status,
        'message' : message
    }

class UserDefinedTask(object):
    def __init__(self, repeat_lib, file_name):
        super(UserDefinedTask, self).__init__()
        self.file_name = file_name
        self.repeat_lib = repeat_lib
        self.executing_module = None

    """
        invoker is the hotkey that invoke this action
    """
    def run(self, invoker):
        print "Running task with file name %s" % self.file_name
        parent_dir = os.path.dirname(self.file_name)
        raw_file_name = os.path.basename(self.file_name)
        raw_file_name = os.path.splitext(raw_file_name)[0] #Remove file extension

        if self.executing_module is None:
            self.executing_module = imp.load_source(raw_file_name, self.file_name)
        self.executing_module.action(self.repeat_lib, invoker)

class TaskManager(object):
    def __init__(self, repeat_lib):
        super(TaskManager, self).__init__()
        assert repeat_lib is not None
        self.repeat_lib = repeat_lib
        self.tasks = {}
        self.base_id = 0

    def _next_id(self):
        self.base_id += 1
        return self.base_id

    def process_message(self, message_id, message):
        action = message['task_action']
        params = message['parameters']

        if action == 'create_task':
            return self.create_task(*params)
        elif action == 'run_task':
            return self.run_task(*params)
        elif action == 'remove_task':
            return self.remove_task(*params)

        return None

    def sync_tasks(self):
        pass

    def create_task(self, file_name):
        if not os.path.isfile(file_name):
            return generate_reply(specifications.FAILURE, 'File %s does not exist' % file_name)
        elif not os.access(file_name, os.X_OK):
            return generate_reply(specifications.FAILURE, 'File %s is not executable' % file_name)

        next_id = self._next_id()
        self.tasks[next_id] = UserDefinedTask(self.repeat_lib, file_name)

        return generate_reply(specifications.SUCCESS, {
                'id' : next_id,
                'file_name' : file_name
            })

    def run_task(self, task_id, invoker):
        if task_id not in self.tasks:
            return generate_reply(specifications.FAILURE, 'Cannot find task id %s' % task_id)
        self.tasks[task_id].run(invoker)
        return generate_reply(specifications.SUCCESS, {
                'id' : task_id,
                'file_name' : self.tasks[task_id].file_name
            })

    def remove_task(self, task_id):
        if task_id not in self.tasks:
            return generate_reply(specifications.SUCCESS, {
                    'id' : task_id,
                    'file_name' : ''
                })

        removing = self.tasks.pop(task_id)
        return generate_reply(specifications.SUCCESS, {
                'id' : task_id,
                'file_name' : removing.file_name
            })
##############################################################################################################################

if __name__ == "__main__":
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
        print "Terminating repeat client..."
        terminate_repeat_client()
