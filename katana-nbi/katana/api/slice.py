from flask import request
from flask_classful import FlaskView, route
from katana.api.mongoUtils import mongoUtils
from katana.api.sliceUtils import sliceUtils
from katana.slice_mapping import slice_mapping

import uuid
from bson.json_util import dumps
from threading import Thread
import time
import logging
import urllib3
import json

from kafka import KafkaProducer, KafkaAdminClient, admin, errors

# Logging Parameters
logger = logging.getLogger(__name__)
file_handler = logging.handlers.RotatingFileHandler(
    'katana.log', maxBytes=10000, backupCount=5)
stream_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s')
stream_formatter = logging.Formatter(
    '%(asctime)s %(name)s %(levelname)s %(message)s')
file_handler.setFormatter(formatter)
stream_handler.setFormatter(stream_formatter)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# Create the kafka producer
tries = 3
exit = False
while not exit:
    try:
        producer = KafkaProducer(
            bootstrap_servers=["kafka:19092"],
            value_serializer=lambda m: json.dumps(m).encode('ascii'))
    except errors.NoBrokersAvailable as KafkaError:
        if tries > 0:
            tries -= 1
            time.sleep(5)
        else:
            logger.error(KafkaError)
    else:
        exit = True
        tries = 3

# Create the Kafka topic
try:
    topic = admin.NewTopic(name="slice", num_partitions=1,
                           replication_factor=1)
    broker = KafkaAdminClient(bootstrap_servers="kafka:19092")
    broker.create_topics([topic])
except errors.TopicAlreadyExistsError:
    print("Exists already")


class SliceView(FlaskView):
    """
    Returns a list of slices and their details,
    used by: `katana slice ls`
    """
    urllib3.disable_warnings()
    route_prefix = '/api/'

    def index(self):
        """
        Returns a list of slices and their details,
        used by: `katana slice ls`
        """
        slice_data = mongoUtils.index("slice")
        return_data = []
        for islice in slice_data:
            return_data.append(dict(_id=islice['_id'],
                                    created_at=islice['created_at'],
                                    status=islice['status']))
        return dumps(return_data), 200

    def get(self, uuid):
        """
        Returns the details of specific slice,
        used by: `katana slice inspect [uuid]`
        """
        data = (mongoUtils.get("slice", uuid))
        if data:
            return dumps(data), 200
        else:
            return "Not Found", 404

    @route('/<uuid>/time')
    def show_time(self, uuid):
        """
        Returns deployment time of a slice
        """
        islice = mongoUtils.get("slice", uuid)
        if islice:
            return dumps(islice["deployment_time"]), 200
        else:
            return "Not Found", 404

    def post(self):
        """
        Add a new slice. The request must provide the slice details.
        used by: `katana slice add -f [yaml file]`
        """
        slice_message = {"action": "add", "message": request.json}
        new_uuid = str(uuid.uuid4())
        request.json['_id'] = new_uuid
        producer.send("slice", value=slice_message)
        # **************************************************************************************
        return "END"

        nest, error_code = slice_mapping.gst_to_nest(request.json)
        if error_code:
            return nest, error_code
        nest['status'] = 'init'
        nest['created_at'] = time.time()  # unix epoch
        nest['deployment_time'] = dict(
            Slice_Deployment_Time='N/A',
            Placement_Time='N/A',
            Provisioning_Time='N/A',
            NS_Deployment_Time='N/A',
            WAN_Deployment_Time='N/A',
            Radio_Configuration_Time='N/A')
        mongoUtils.add("slice", request.json)
        # background work
        # temp hack from:
        # https://stackoverflow.com/questions/48994440/execute-a-function-after-flask-returns-response
        # might be replaced with Celery...

        thread = Thread(target=sliceUtils.do_work, kwargs={'nest_req': nest})
        thread.start()

        return new_uuid, 201

    def delete(self, uuid):
        """
        Delete a specific slice.
        used by: `katana slice rm [uuid]`
        """

        # check if slice uuid exists
        delete_json = mongoUtils.get("slice", uuid)

        if not delete_json:
            return "Error: No such slice: {}".format(uuid), 404
        else:
            delete_thread = Thread(target=sliceUtils.delete_slice,
                                   kwargs={'slice_json': delete_json})
            delete_thread.start()
            return "Deleting {0}".format(uuid), 200

    # def put(self, uuid):
    #     """
    #     Update the details of a specific slice.
    #     used by: `katana slice update -f [yaml file] [uuid]`
    #     """
    #     request.json['_id'] = uuid
    #     result = mongoUtils.update("slice", uuid, request.json)

    #     if result == 1:
    #         return uuid
    #     elif result == 0:
    #         # if no object was modified, return error
    #         return "Error: No such slice: {}".format(uuid)