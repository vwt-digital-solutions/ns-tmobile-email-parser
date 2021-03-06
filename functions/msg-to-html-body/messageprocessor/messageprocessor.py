from config import TOPIC_PROJECT_ID, TOPIC_NAME, HTML_TEMPLATE_PATHS, \
                   TEMPLATE_PATH_FIELD, RECIPIENT_MAPPING_MESSAGE_FIELD, \
                   RECIPIENT_MAPPING, SENDER
import logging
import json
import os
from jinja2 import Template
import datetime
from gobits import Gobits
from google.cloud import pubsub_v1
from .firestoreprocessor import FirestoreProcessor

logging.basicConfig(level=logging.INFO)


class MessageProcessor(object):

    def __init__(self):
        self.data_selector = os.environ.get('DATA_SELECTOR', 'Required environment variable DATA_SELECTOR is missing')
        self.topic_project_id = TOPIC_PROJECT_ID
        self.topic_name = TOPIC_NAME
        self.html_template_field = TEMPLATE_PATH_FIELD
        self.html_template_paths = HTML_TEMPLATE_PATHS
        self.recipient_mapping_message_field = RECIPIENT_MAPPING_MESSAGE_FIELD
        self.recipient_mapping = RECIPIENT_MAPPING
        self.sender = SENDER
        self.gcp_firestore = FirestoreProcessor()

    def process(self, payload):
        # Get message
        message = payload[self.data_selector]
        # Message to HTML body
        html_body, subject = self.message_to_html(message)
        if not html_body or not subject:
            logging.error("Message was not processed")
            return False
        # Make topic message
        count = 0
        for field in message:
            message_root = field
            count = count + 1
        if count > 1:
            logging.error("Message has multiple roots")
            return False
        recipient_mapping_field_dict = message.get(message_root)
        recipient_mapping_field_message = recipient_mapping_field_dict.get(self.recipient_mapping_message_field)
        if not recipient_mapping_field_message:
            logging.error(f"The field {self.recipient_mapping_message_field} could not be found in the message")
            return False
        topic_message = self.make_topic_msg(recipient_mapping_field_message, html_body, subject)
        if not topic_message:
            logging.error("Topic message was not made")
            return False
        # Make gobits
        gobits = Gobits()
        # Send message to topic
        return_bool = self.publish_to_topic(subject, topic_message, gobits)
        if return_bool is False:
            logging.error("Message was not processed")
            return False
        else:
            logging.info("Message was processed")
        return True

    def make_topic_msg(self, recipient_mapping_field_message, body, subject):
        now = datetime.datetime.now()
        now_iso = now.isoformat()
        recipient = self.get_recipient(recipient_mapping_field_message)
        if not recipient:
            logging.error("Something went wrong in getting the recipient from the Firestore")
            return None
        message = {
            "sent_on": now_iso,
            "received_on": "",
            "sender": self.sender,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "attachments": []
        }
        return message

    def get_recipient(self, recipient_mapping_field):
        recipient_dict = self.recipient_mapping.get(recipient_mapping_field)
        if not recipient_dict:
            logging.error(f"No recipient dictionary belonging to field {recipient_mapping_field}"
                          " could be found in 'recipient_mapping' in config")
            return None
        collection_name = recipient_dict.get("firestore_collection_name")
        if not collection_name:
            logging.error("'firestore_collection_name' not defined in recipient mapping in config")
            return None
        firestore_ids = recipient_dict.get("firestore_ids")
        if not firestore_ids:
            logging.error("'firestore_ids' not defined in recipient mapping in config")
            return None
        firestore_value = recipient_dict.get("firestore_value")
        if not firestore_value:
            logging.error("'firestore_value' not defined in recipient mapping in config")
            return None
        succeeded, fs_value = self.gcp_firestore.get_value(collection_name, firestore_ids,
                                                           firestore_value)
        if succeeded:
            return fs_value
        logging.error("Recipient could not be found based on recipient mapping defined in config")
        return None

    def message_to_html(self, message):
        if not self.html_template_paths:
            logging.error("HTML template path is not defined in config")
            return None, None
        message_after_root = {}
        count = 0
        # Get part of message after the root
        for after_root in message:
            if isinstance(message[after_root], dict):
                message_after_root = message[after_root]
            count = count + 1
        if count > 1:
            logging.error("The message contains multiple roots")
            return None, None
        if not message_after_root:
            logging.error("The message does not contain a root")
            return None, None
        # Get message field
        temp_msg_field = message_after_root.get(self.html_template_field)
        if not temp_msg_field:
            logging.error("Could not get right message field to get template")
            return None, None
        # Get the right template
        temp_info = self.html_template_paths.get(temp_msg_field)
        template_path = temp_info.get('template_path')
        if not template_path:
            logging.error(f"Template paths in config do not have field {temp_msg_field}")
            return None, None
        template_args = temp_info.get('template_args')
        kwargs = {}
        for arg_field in template_args:
            # Get value
            arg_value = ""
            arg_field_values = template_args[arg_field]
            for arg_field_value in arg_field_values:
                # Check if value is MESSAGE_FIELD
                if arg_field_values[arg_field_value] == "MESSAGE_FIELD":
                    # Get value from message
                    arg_value = message_after_root.get(arg_field_value)
                    # Check if format is datetime
                    arg_value_format = arg_field_values.get("arg_field_format")
                    if arg_value_format:
                        if arg_value_format == "DATETIME":
                            arg_value = datetime.datetime.strptime(arg_value, '%Y%m%d%H%M%S')
                kwargs.update({arg_field: arg_value})
        with open(template_path) as file_:
            template = Template(file_.read())
        body = template.render(kwargs)
        mail_subject = temp_info.get('mail_subject')
        if not mail_subject:
            logging.error(f"Field mail_subject could not be found in field {temp_msg_field}")
            return None, None
        # Get subject
        subject = ""
        for field in mail_subject:
            to_add = ""
            if mail_subject[field] == "HARDCODED":
                to_add = field
            elif mail_subject[field] == "MESSAGE_FIELD":
                subject_msg_field = message_after_root.get(field)
                # Check if subject field is found
                if not subject_msg_field:
                    logging.error(f"Field {field} could not be found in message")
                    return None, None
                to_add = subject_msg_field
            # If subject is not empty
            if subject:
                subject = f"{subject} {to_add}"
            else:
                subject = to_add
        return body, subject

    def publish_to_topic(self, subject, message, gobits):
        msg = {
            "gobits": [gobits.to_json()],
            "email": message
        }
        try:
            # Publish to topic
            publisher = pubsub_v1.PublisherClient()
            topic_path = "projects/{}/topics/{}".format(
                self.topic_project_id, self.topic_name)
            future = publisher.publish(
                topic_path, bytes(json.dumps(msg).encode('utf-8')))
            future.add_done_callback(
                lambda x: logging.debug('Published to export email with subject {}'.format(subject))
            )
            return True
        except Exception as e:
            logging.exception('Unable to publish parsed email ' +
                              'to topic because of {}'.format(e))
        return False
