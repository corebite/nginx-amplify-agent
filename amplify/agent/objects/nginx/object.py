# -*- coding: utf-8 -*-
from amplify.agent.collectors.nginx.accesslog import NginxAccessLogsCollector
from amplify.agent.collectors.nginx.config import NginxConfigCollector
from amplify.agent.collectors.nginx.errorlog import NginxErrorLogsCollector
from amplify.agent.collectors.nginx.metrics import NginxMetricsCollector

from amplify.agent.common.context import context
from amplify.agent.common.util import host, http
from amplify.agent.data.eventd import INFO
from amplify.agent.objects.abstract import AbstractObject
from amplify.agent.objects.nginx.binary import nginx_v
from amplify.agent.objects.nginx.config.config import NginxConfig
from amplify.agent.objects.nginx.filters import Filter

__author__ = "Mike Belov"
__copyright__ = "Copyright (C) Nginx, Inc. All rights reserved."
__credits__ = ["Mike Belov", "Andrei Belov", "Ivan Poluyanov", "Oleg Mamontov", "Andrew Alexeev", "Grant Hulegaard"]
__license__ = ""
__maintainer__ = "Mike Belov"
__email__ = "dedm@nginx.com"


class NginxObject(AbstractObject):
    type = 'nginx'

    def __init__(self, **kwargs):
        super(NginxObject, self).__init__(**kwargs)

        # Have to override intervals here because new container sub objects.
        self.intervals = context.app_config['containers'].get('nginx', {}).get('poll_intervals', {'default': 10})

        self.root_uuid = self.data.get(
            'root_uuid') or context.objects.root_object.uuid if context.objects.root_object else None
        self.local_id_cache = self.data['local_id']  # Assigned by manager
        self.pid = self.data['pid']
        self.version = self.data['version']
        self.workers = self.data['workers']
        self.prefix = self.data['prefix']
        self.bin_path = self.data['bin_path']
        self.conf_path = self.data['conf_path']

        # agent config
        default_config = context.app_config['containers']['nginx']
        self.upload_config = self.data.get('upload_config') or default_config.get('upload_config', False)
        self.run_config_test = self.data.get('run_test') or default_config.get('run_test', False)
        self.upload_ssl = self.data.get('upload_ssl') or default_config.get('upload_ssl', False)

        # nginx -V data
        self.parsed_v = nginx_v(self.bin_path)

        # filters
        self.filters = [Filter(**raw_filter) for raw_filter in self.data.get('filters') or []]

        self.config = NginxConfig(self.conf_path, prefix=self.prefix)
        self.config.full_parse()

        # plus status
        self.plus_status_external_url, self.plus_status_internal_url = self.get_alive_plus_status_urls()
        self.plus_status_enabled = True if (self.plus_status_external_url or self.plus_status_internal_url) else False

        # stub status
        self.stub_status_url = self.get_alive_stub_status_url()
        self.stub_status_enabled = True if self.stub_status_url else False

        self.processes = []

        self.collectors = []
        self._setup_meta_collector()
        self._setup_metrics_collector()
        self._setup_config_collector()
        self._setup_access_logs()
        self._setup_error_logs()

    @property
    def definition(self):
        # Type is hard coded so it is not different from ContainerNginxObject.
        return {'type': 'nginx', 'local_id': self.local_id, 'root_uuid': self.root_uuid}

    def get_alive_stub_status_url(self):
        """
        Tries to get alive stub_status url
        Records some events about it
        :return:
        """
        urls_to_check = self.config.stub_status_urls

        if 'stub_status' in context.app_config.get('nginx', {}):
            predefined_uri = context.app_config['nginx']['stub_status']
            urls_to_check.append(http.resolve_uri(predefined_uri))

        stub_status_url = self.__get_alive_status(urls_to_check)
        if stub_status_url:
            # Send stub detected event
            self.eventd.event(
                level=INFO,
                message='nginx stub_status detected, %s' % stub_status_url
            )
        else:
            self.eventd.event(
                level=INFO,
                message='nginx stub_status not found in nginx config'
            )
        return stub_status_url

    def get_alive_plus_status_urls(self):
        """
        Tries to get alive plus urls
        There are two types of plus status urls: internal and external
        - internal are for the agent and usually they have the localhost ip in address
        - external are for the browsers and usually they have a normal server name

        Returns a tuple of str or Nones - (external_url, internal_url)

        Even if external status url is not responding (cannot be accesible from the host)
        we should return it to show in our UI

        :return: (str or None, str or None)
        """
        internal_urls = self.config.plus_status_internal_urls
        external_urls = self.config.plus_status_external_urls

        if 'plus_status' in context.app_config.get('nginx', {}):
            predefined_uri = context.app_config['nginx']['plus_status']
            internal_urls.append(http.resolve_uri(predefined_uri))

        internal_status_url = self.__get_alive_status(internal_urls, json=True)
        if internal_status_url:
            self.eventd.event(
                level=INFO,
                message='nginx internal plus_status detected, %s' % internal_status_url
            )

        external_status_url = self.__get_alive_status(external_urls, json=True)
        if len(self.config.plus_status_external_urls) > 0:
            if not external_status_url:
                external_status_url = 'http://%s' % self.config.plus_status_external_urls[0]

            self.eventd.event(
                level=INFO,
                message='nginx external plus_status detected, %s' % external_status_url
            )

        return external_status_url, internal_status_url

    def __get_alive_status(self, url_list, json=False):
        """
        Tries to find alive status url
        Returns first alive url or None if all founded urls are not responding

        :param url_list: [] of urls
        :param json: bool - will try to encode json if True
        :return: None or str
        """
        for url in url_list:
            for proto in ('http://', 'https://'):
                full_url = '%s%s' % (proto, url) if not url.startswith('http') else url
                try:
                    status_response = context.http_client.get(full_url, timeout=0.5, json=json, log=False)
                    if status_response:
                        if json or 'Active connections' in status_response:
                            return full_url
                    else:
                        context.log.debug('bad response from stub/plus status url %s' % full_url)
                except:
                    context.log.debug('bad response from stub/plus status url %s' % full_url)
        return None

    def _setup_meta_collector(self):
        collector_cls = self._import_collector_class('nginx', 'meta')
        self.collectors.append(
            collector_cls(object=self, interval=self.intervals['meta'])
        )

    def _setup_metrics_collector(self):
        collector_cls = self._import_collector_class('nginx', 'metrics')
        self.collectors.append(
            collector_cls(object=self, interval=self.intervals['metrics'])
        )

    def _setup_config_collector(self):
        self.collectors.append(
            NginxConfigCollector(
                object=self, interval=self.intervals['configs'],
            )
        )

    def _setup_access_logs(self):
        # access logs
        for log_filename, format_name in self.config.access_logs.iteritems():
            log_format = self.config.log_formats.get(format_name)
            try:
                self.collectors.append(
                    NginxAccessLogsCollector(
                        object=self,
                        interval=self.intervals['logs'],
                        filename=log_filename,
                        log_format=log_format,
                    )
                )

                # Send access log discovery event.
                self.eventd.event(level=INFO, message='nginx access log %s found' % log_filename)
            except (IOError, OSError) as e:
                exception_name = e.__class__.__name__
                context.log.warning(
                    'failed to start reading log %s due to %s (maybe has no rights?)' %
                    (log_filename, exception_name)
                )
                context.log.debug('additional info:', exc_info=True)

    def _setup_error_logs(self):
        # error logs
        for log_filename, log_level in self.config.error_logs.iteritems():
            try:
                self.collectors.append(
                    NginxErrorLogsCollector(
                        object=self,
                        interval=self.intervals['logs'],
                        filename=log_filename,
                        level=log_level
                    )
                )

                # Send error log discovery event.
                self.eventd.event(level=INFO, message='nginx error log %s found' % log_filename)
            except (OSError, IOError) as e:
                exception_name = e.__class__.__name__
                context.log.warning(
                    'failed to start reading log %s due to %s (maybe has no rights?)' %
                    (log_filename, exception_name)
                )
                context.log.debug('additional info:', exc_info=True)


class ContainerNginxObject(NginxObject):
    type = 'container_nginx'
