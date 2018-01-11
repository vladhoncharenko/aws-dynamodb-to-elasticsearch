import base64
import datetime
import json
import logging
import os
import time
import traceback
import urllib
import urlparse

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import get_credentials
from botocore.endpoint import BotocoreHTTPSession
from botocore.session import Session
from boto3.dynamodb.types import TypeDeserializer


# The following parameters are required to configure the ES cluster
ES_ENDPOINT = 'ES_ENDPOINT HERE'

# The following parameters can be optionally customized
DOC_TABLE_FORMAT = '{}'         # Python formatter to generate index name from the DynamoDB table name
DOC_TYPE_FORMAT = '{}_type'     # Python formatter to generate type name from the DynamoDB table name, default is to add '_type' suffix
ES_REGION = None                # If not set, use the runtime lambda region
ES_MAX_RETRIES = 3              # Max number of retries for exponential backoff
DEBUG = True                    # Set verbose debugging information

print "Streaming to ElasticSearch"
logger = logging.getLogger()
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


# Subclass of boto's TypeDeserializer for DynamoDB to adjust for DynamoDB Stream format.
class StreamTypeDeserializer(TypeDeserializer):
   def _deserialize_n(self, value):
       return float(value)

   def _deserialize_b(self, value):
       return value  # Already in Base64


class ES_Exception(Exception):
   '''Exception capturing status_code from Client Request'''
   status_code = 0
   payload = ''

   def __init__(self, status_code, payload):
       self.status_code = status_code
       self.payload = payload
       Exception.__init__(self, 'ES_Exception: status_code={}, payload={}'.format(status_code, payload))


# Low-level POST data to Amazon Elasticsearch Service generating a Sigv4 signed request
def post_data_to_es(payload, region, creds, host, path, method='POST', proto='https://'):
   '''Post data to ES endpoint with SigV4 signed http headers'''
   req = AWSRequest(method=method, url=proto + host + urllib.quote(path), data=payload, headers={'Host': host, 'Content-Type' : 'application/json'})
   SigV4Auth(creds, 'es', region).add_auth(req)
   http_session = BotocoreHTTPSession()
   res = http_session.send(req.prepare())
   if res.status_code >= 200 and res.status_code <= 299:
       return res._content
   else:
       raise ES_Exception(res.status_code, res._content)


# High-level POST data to Amazon Elasticsearch Service with exponential backoff
# according to suggested algorithm: http://docs.aws.amazon.com/general/latest/gr/api-retries.html
def post_to_es(payload):
   '''Post data to ES cluster with exponential backoff'''

   # Get aws_region and credentials to post signed URL to ES
   es_region = ES_REGION or os.environ['AWS_REGION']
   session = Session({'region': es_region})
   creds = get_credentials(session)
   es_url = urlparse.urlparse(ES_ENDPOINT)
   es_endpoint = es_url.netloc or es_url.path  # Extract the domain name in ES_ENDPOINT

   # Post data with exponential backoff
   retries = 0
   while retries < ES_MAX_RETRIES:
       if retries > 0:
           seconds = (2 ** retries) * .1
           time.sleep(seconds)

       try:
           es_ret_str = post_data_to_es(payload, es_region, creds, es_endpoint, '/_bulk')
           es_ret = json.loads(es_ret_str)

           if es_ret['errors']:
               logger.error('ES post unsuccessful, errors present, took=%sms', es_ret['took'])
               # Filter errors
               es_errors = [item for item in es_ret['items'] if item.get('index').get('error')]
               logger.error('List of items with errors: %s', json.dumps(es_errors))
           else:
               logger.info('ES post successful, took=%sms', es_ret['took'])
           break  # Sending to ES was ok, break retry loop
       except ES_Exception as e:
           if (e.status_code >= 500) and (e.status_code <= 599):
               retries += 1  # Candidate for retry
           else:
               raise  # Stop retrying, re-raise exception


# Extracts the DynamoDB table from an ARN
# ex: arn:aws:dynamodb:eu-west-1:123456789012:table/table-name/stream/2015-11-13T09:23:17.104 should return 'table-name'
def get_table_name_from_arn(arn):
   return arn.split(':')[5].split('/')[1]


# Compute a compound doc index from the key(s) of the object in lexicographic order: "k1=key_val1|k2=key_val2"
def compute_doc_index(keys_raw, deserializer):
   index = []
   for key in sorted(keys_raw):
       index.append('{}={}'.format(key, deserializer.deserialize(keys_raw[key])))
   return '|'.join(index)


def _lambda_handler(event, context):
   records = event['Records']
   now = datetime.datetime.utcnow()

   ddb_deserializer = StreamTypeDeserializer()
   es_actions = []  # Items to be added/updated/removed from ES - for bulk API
   cnt_insert = cnt_modify = cnt_remove = 0
   for record in records:
       # Handle both native DynamoDB Streams or Streams data from Kinesis (for manual replay)
       if record.get('eventSource') == 'aws:dynamodb':
           ddb = record['dynamodb']
           ddb_table_name = get_table_name_from_arn(record['eventSourceARN'])
           doc_seq = ddb['SequenceNumber']
       elif record.get('eventSource') == 'aws:kinesis':
           ddb = json.loads(base64.b64decode(record['kinesis']['data']))
           ddb_table_name = ddb['SourceTable']
           doc_seq = record['kinesis']['sequenceNumber']
       else:
           logger.error('Ignoring non-DynamoDB event sources: %s', record.get('eventSource'))
           continue

       # Compute DynamoDB table, type and index for item
       doc_table = DOC_TABLE_FORMAT.format(ddb_table_name.lower())  # Use formatter
       doc_type = DOC_TYPE_FORMAT.format(ddb_table_name.lower())    # Use formatter
       doc_index = compute_doc_index(ddb['Keys'], ddb_deserializer)

       # Dispatch according to event TYPE
       event_name = record['eventName'].upper()  # INSERT, MODIFY, REMOVE

       # Treat events from a Kinesis stream as INSERTs
       if event_name == 'AWS:KINESIS:RECORD':
           event_name = 'INSERT'

       # Update counters
       if event_name == 'INSERT':
           cnt_insert += 1
       elif event_name == 'MODIFY':
           cnt_modify += 1
       elif event_name == 'REMOVE':
           cnt_remove += 1
       else:
           logger.warning('Unsupported event_name: %s', event_name)

       # If DynamoDB INSERT or MODIFY, send 'index' to ES
       if (event_name == 'INSERT') or (event_name == 'MODIFY'):
           if 'NewImage' not in ddb:
               logger.warning('Cannot process stream if it does not contain NewImage')
               continue
           # Deserialize DynamoDB type to Python types
           doc_fields = ddb_deserializer.deserialize({'M': ddb['NewImage']})
           # Add metadata
           doc_fields['@timestamp'] = now.isoformat()
           doc_fields['@SequenceNumber'] = doc_seq

           # Generate JSON payload
           doc_json = json.dumps(doc_fields)

           # Generate ES payload for item
           action = {'index': {'_index': doc_table, '_type': doc_type, '_id': doc_index}}
           es_actions.append(json.dumps(action))  # Action line with 'index' directive
           es_actions.append(doc_json)            # Payload line

       # If DynamoDB REMOVE, send 'delete' to ES
       elif event_name == 'REMOVE':
           action = {'delete': {'_index': doc_table, '_type': doc_type, '_id': doc_index}}
           es_actions.append(json.dumps(action))

   # Prepare bulk payload
   es_actions.append('')  # Add one empty line to force final \n
   es_payload = '\n'.join(es_actions)

   post_to_es(es_payload)  # Post to ES with exponential backoff


# Global lambda handler - catches all exceptions to avoid dead letter in the DynamoDB Stream
def lambda_handler(event, context):
   try:
       return _lambda_handler(event, context)
   except Exception:
       logger.error(traceback.format_exc())
