# Copyright 2014 Michael Malocha <michael@knockrentals.com>
#
# Expanded from the work by Julien Duponchelle <julien@duponchelle.info>.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Elastic Search Pipeline for scrappy expanded with support for multiple items"""

from datetime import datetime
from elasticsearch import Elasticsearch, helpers
from six import string_types

import logging
import hashlib
import types
import re
import os
from collections import namedtuple

class InvalidSettingsException(Exception):
    pass

_VersionInfo = namedtuple('_VersionInfo', ['major', 'minor', 'patch', 'full'])

class ElasticSearchPipeline(object):
    settings = None
    es = None
    items_buffer = []

    @classmethod
    def validate_settings(cls, settings):
        def validate_setting(setting_key):
            if settings[setting_key] is None:
                raise InvalidSettingsException('%s is not defined in settings.py' % setting_key)

        required_settings = {'ELASTICSEARCH_INDEX'}

        for required_setting in required_settings:
            validate_setting(required_setting)

    @staticmethod
    def _get_version(es):
        logging.info('Checking Elasticsearch version...')
        # Perform raw request and unpack status, body
        info = es.info()
        logging.info('%s', info)
        vers = info['version']['number']
        match = re.match(r'(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)', vers)
        major_num = int(match['major'])
        minor_num = int(match['minor'])
        patch_num = int(match['patch'])
        logging.info('Elasticsearch version is: %s.%s.%s' % (major_num, minor_num, patch_num))
        return _VersionInfo(major_num, minor_num, patch_num, vers)

    @classmethod
    def validate_vers_spec_settings(cls, settings, es):
        """Validate with version-specific rules
        """
        def require_setting(setting_key, version):
            if settings[setting_key] is None:
                raise InvalidSettingsException('%s is not defined in settings.py when server version is %s'
                                               % (setting_key, version.full))

        logging.debug('In validate_vers_spec_settings()')
        vers = cls._get_version(es)

        # Require only if version is less than 6.2.d.
        if vers.major < 6 or (vers.major == 6 and vers.minor < 2):
            require_setting('ELASTICSEARCH_TYPE', vers)

    @classmethod
    def init_es_client(cls, crawler_settings):

        es_timeout = crawler_settings.get('ELASTICSEARCH_TIMEOUT',60)
        es_cloud_id = crawler_settings.get('ELASTICSEARCH_CLOUD_ID')
        es_api_key = crawler_settings.get('ELASTICSEARCH_API_KEY')
        
        try:
            logging.info('Create Elasticsearch client in ScrapyElasticsearchPipeline')
            es = Elasticsearch(
                cloud_id=es_cloud_id,
                api_key=es_api_key,
                request_timeout=es_timeout,
                headers = {
                    "Accept": "application/vnd.elasticsearch+json; compatible-with=8",
                    "Content-Type": "application/vnd.elasticsearch+json; compatible-with=8"
                }
            )
            info = es.info()
            logging.info('%s', info)

        except Exception as e:
            logging.info('Error creating Elasticsearch client in ScrapyElasticsearchPipeline')

        return es

    @classmethod
    def from_crawler(cls, crawler):
        ext = cls()
        ext.settings = crawler.settings

        cls.validate_settings(ext.settings)
        ext.es = cls.init_es_client(crawler.settings)
        cls.validate_vers_spec_settings(ext.settings, ext.es)
        return ext

    def process_unique_key(self, unique_key):
        if isinstance(unique_key, (list, tuple)):
            unique_key = unique_key[0].encode('utf-8')
        elif isinstance(unique_key, string_types):
            unique_key = unique_key.encode('utf-8')
        else:
            raise Exception('unique key must be str or unicode')

        return unique_key

    def get_id(self, item):
        item_unique_key = item[self.settings['ELASTICSEARCH_UNIQ_KEY']]
        if isinstance(item_unique_key, list):
            item_unique_key = '-'.join(item_unique_key)

        unique_key = self.process_unique_key(item_unique_key)
        item_id = hashlib.sha1(unique_key).hexdigest()
        return item_id

    def index_item(self, item):

        index_name = self.settings['ELASTICSEARCH_INDEX']
        index_suffix_format = self.settings.get('ELASTICSEARCH_INDEX_DATE_FORMAT', None)
        index_suffix_key = self.settings.get('ELASTICSEARCH_INDEX_DATE_KEY', None)
        index_suffix_key_format = self.settings.get('ELASTICSEARCH_INDEX_DATE_KEY_FORMAT', None)

        if index_suffix_format:
            if index_suffix_key and index_suffix_key_format:
                dt = datetime.strptime(item[index_suffix_key], index_suffix_key_format)
            else:
                dt = datetime.now()
            index_name += "-" + datetime.strftime(dt,index_suffix_format)
        elif index_suffix_key:
            index_name += "-" + index_suffix_key

        index_action = {
            '_index': index_name,
            '_source': dict(item)
        }

        # The ES roadmap migrates to a typeless API with ES 7 and later
        if 'ELASTICSEARCH_TYPE' in self.settings:
            index_action['_type'] = self.settings['ELASTICSEARCH_TYPE']

        if self.settings['ELASTICSEARCH_UNIQ_KEY'] is not None:
            item_id = self.get_id(item)
            index_action['_id'] = item_id
            logging.debug('Generated unique key %s' % item_id)

        # logging.info('SEND THIS ITEM TO ES: %s', index_action)

        self.items_buffer.append(index_action)

        if len(self.items_buffer) >= self.settings.get('ELASTICSEARCH_BUFFER_LENGTH', 500):
            self.send_items()
            self.items_buffer = []

    def send_items(self):

        sendItems = helpers.streaming_bulk(self.es, self.items_buffer)

        try:
            for ok, result in sendItems:

                if not ok:
                    __, result = result.popitem()
                    logging.info('There was an issue sending item to Elasticsearch: (%s) %s', __, result)
                    
        except Exception as e:
            logging.info('Error iterating through streaming_bulk result: %s', e)
            
    def process_item(self, item, spider):
        if isinstance(item, types.GeneratorType) or isinstance(item, list):
            for each in item:
                self.process_item(each, spider)
        else:
            self.index_item(item)
            logging.debug('Item sent to Elastic Search %s' % self.settings['ELASTICSEARCH_INDEX'])
            return item

    def close_spider(self, spider):
        if len(self.items_buffer):
            self.send_items()

