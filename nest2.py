#!/usr/bin/env python3

import polyinterface
import sys
import json
from os.path import join, expanduser
from pathlib import Path
import http.client
from threading import Thread
import urllib3
import sseclient
from urllib.parse import urlparse
import certifi

from converters import id_2_addr
from node_types import Thermostat, ThermostatC, Structure

LOGGER = polyinterface.LOGGER

NEST_API_URL = 'https://developer-api.nest.com'

CACHEFILE = join(expanduser("~") + '/.polyglot/.nest_auth')

class Controller(polyinterface.Controller):
    def __init__(self, polyglot):
        super(Controller, self).__init__(polyglot)
        self.name = 'Nest Controller'
        self.address = 'nestctrl'
        self.primary = self.address
        self.auth_conn = None
        self.api_conn = None
        self.auth_token = None
        self.authenticated = None
        self.stream_thread = None
        self.data = None
        self.discovery = None
        
    def start(self):
        LOGGER.info('Starting Nest2 Polyglot v2 NodeServer')
        cache_file = Path(join(expanduser("~") + '/.nest_poly'))

        if 'token' not in self.polyConfig['customParams']:
            if cache_file.is_file():
                LOGGER.info('Attempting to read auth_token from ~/.nest_poly')
                with cache_file.open() as f:
                    cache_data = json.load(f)
                    if 'auth_token' in cache_data:
                        self.auth_token = cache_data['auth_token']
                    f.close()
            else:
                LOGGER.debug('Cached token is not found')
        else:
            LOGGER.debug('Using auth_token from the database')
            self.auth_token = self.polyConfig['customParams']['token']
            
        if self.auth_token is None:
            with open('server.json') as sf:
                server_data = json.load(sf)
                sf.close()
            if 'pin' in self.polyConfig['customParams']:
                LOGGER.debug('PIN code obtained, attempting to get a token')
                auth_conn = http.client.HTTPSConnection("api.home.nest.com")
                payload = "code="+self.polyConfig['customParams']['pin']+"&client_id=" + \
                          server_data['api_client']+"&client_secret="+server_data['api_key']+ \
                          "&grant_type=authorization_code"
                headers = { 'content-type': "application/x-www-form-urlencoded" }
                auth_conn.request("POST", "/oauth2/access_token", payload, headers)
                res = auth_conn.getresponse()
                raw_data = res.read()
                data = json.loads(raw_data.decode("utf-8"))
                auth_conn.close()
                if 'auth_token' in data:
                    LOGGER.debug('Received authentication token, saving...')
                    self.auth_token = data['auth_token']
                    with cache_file.open() as cf:
                        json.dump(data, cf)
                        cf.close()
                else:
                    LOGGER.error('Failed to get auth_token')
            else:
                self._pinPrompt(server_data['api_client'])
        else:
            self.discover()
            self._checkStreaming()

    def longPoll(self):
        self._checkStreaming()

    def _checkStreaming(self):
        if self.auth_token is None or self.discovery == True:
            return False
        if self.stream_thread is None:
            LOGGER.debug('Starting REST Streaming thread for the first time.')
            self._startStreaming()
        else:
            if self.stream_thread.isAlive():
                return True
            else:
                LOGGER.warning('REST Streaming thread died, attempting to restart.')
                self._startStreaming()
        return True

    def _startStreaming(self):
        self.stream_thread = Thread(target=self._streamingProc, daemon=True)
        self.stream_thread.start()
        
    def _streamingProc(self):
        headers = {
            'Authorization': "Bearer {0}".format(self.auth_token),
            'Accept': 'text/event-stream'
        }
        url = NEST_API_URL
        http = urllib3.PoolManager(cert_reqs='CERT_REQUIRED',ca_certs=certifi.where())
        response = http.request('GET', url, headers=headers, preload_content=False)
        client = sseclient.SSEClient(response)
        for event in client.events(): # returns a generator
            event_type = event.event
            if event_type == 'open': # not always received here
                LOGGER.debug('The event stream has been opened')
            elif event_type == 'put':
                LOGGER.debug('The data has changed (or initial data sent)')
                event_data = json.loads(event.data)
                self.data = event_data['data']
                for node in self.nodes:
                    self.nodes[node].update()
            elif event_type == 'keep-alive':
                LOGGER.debug('No data updates. Receiving an HTTP header to keep the connection open.')
            elif event_type == 'auth_revoked':
                LOGGER.warning('The API authorization has been revoked. {}'.format(event.data))
                self.auth_token = None
                return False
            elif event_type == 'error':
                LOGGER.error('Error occurred, such as connection closed: {}'.format(event.data))
                return False
            else:
                LOGGER.error('REST Streaming: Unhandled event')
                return False
                
    def update(self):
        pass
        
    def _pinPrompt(self, client_id):    
        LOGGER.info('Go to https://home.nest.com/login/oauth2?client_id={}&state=poly to authorize. '.format(client_id) + \
                    'Then enter the code under the Custom Parameters section. ' + \
                    'Create a key called "pin" with the code as the value, then restart the NodeServer.')
    
    def discover(self, command = None):
        LOGGER.info('Discovering Nest Products...')
        if self.auth_token is None:
            return False
        
        if not self.getState():
            return False

        self.discovery = True
        
        ''' Copy initial data if REST Streaming is not active yet '''
        if self.data is None:
            self.data = self.api_data
            
        if 'structures' not in self.api_data:
            LOGGER.error('Nest API did not return any structures')
            self.discovery = False
            return False

        structures = self.api_data['structures']
        LOGGER.info("Found {} structure(s)".format(len(structures)))

        for struct_id, struct in structures.items():
            address = id_2_addr(struct_id)
            LOGGER.info("Id: {}, Name: {}".format(address, struct['name']))
            if address not in self.nodes:
                self.addNode(Structure(self, self.address, address, struct['name'], struct_id, struct))

        if 'thermostats' in self.api_data['devices']:
            thermostats = self.api_data['devices']['thermostats']
            LOGGER.info("Found {} thermostat(s)".format(len(thermostats)))

            for tstat_id, tstat in thermostats.items():
                address = id_2_addr(tstat_id)
                LOGGER.info("Id: {}, Name: {}".format(address, tstat['name_long']))
                if address not in self.nodes:
                    if tstat['temperature_scale'] == 'F':
                        self.addNode(Thermostat(self, self.address, address, tstat['name'], tstat_id, tstat))
                    else:
                        self.addNode(ThermostatC(self, self.address, address, tstat['name'], tstat_id, tstat))

        if 'smoke_co_alarms' in self.api_data['devices']:
            smokedets = self.api_data['devices']['smoke_co_alarms']
            LOGGER.info("Found {} smoke detector(s)".format(len(smokedets)))

            for smkdet_id, smkdet in smokedets.items():
                address = id_2_addr(smkdet_id)
                LOGGER.info("Id: {}, Name: {}".format(address, smkdet['name_long']))
                
        if 'cameras' in self.api_data['devices']:
            cams = self.api_data['devices']['cameras']
            LOGGER.info("Found {} smoke camera(s)".format(len(cams)))

            for cam_id, camera in cams.items():
                address = id_2_addr(cam_id)
                LOGGER.info("Id: {}, Name: {}".format(address, camera['name_long']))

        self.discovery = False

    def getState(self):
        headers = {'authorization': "Bearer {0}".format(self.auth_token)}

        if self.api_conn is None:
            ''' Establish a fresh connection '''
            LOGGER.debug('getState: Attempting to open a connection to the Nest API endpoint')
            self.api_conn = http.client.HTTPSConnection("developer-api.nest.com")

        ''' re-use an existing connection '''
        self.api_conn.request("GET", "/", headers=headers)
        response = self.api_conn.getresponse()

        if response.status == 307:
            redirectLocation = urlparse(response.getheader("location"))
            LOGGER.debug("Redirected to: {}".format(redirectLocation.geturl()))
            self.api_conn = http.client.HTTPSConnection(redirectLocation.netloc)
            self.api_conn.request("GET", "/", headers=headers)
            response = self.api_conn.getresponse()
            LOGGER.debug('Response status: {}'.format(response.status))
            if response.status != 200:
                LOGGER.error('Redirect with non 200 response')
                self.api_conn.close()
                self.api_conn = None
                return False
        self.api_data = json.loads(response.read().decode("utf-8"))	
        return True
    
    def sendChange(self, url, payload):
        ''' url = "/devices/thermostats/device_id"
            payload = "{\"temperature_scale\": \"F\"}"
        '''
        if self.api_conn is None:
            LOGGER.info('sendChange: API Connection is not yet active')
            self.api_conn = http.client.HTTPSConnection("developer-api.nest.com")

        command = json.dumps(payload, separators=(',', ': '))
        headers = {'authorization': "Bearer {0}".format(self.auth_token)}
        LOGGER.debug('Sending {} to {}'.format(command, url))
        self.api_conn.request("PUT", url, command, headers)

        response = self.api_conn.getresponse()

        if response.status == 307:
            redirectLocation = urlparse(response.getheader("location"))
            LOGGER.debug("Redirected to: {}".format(redirectLocation.geturl()))
            self.api_conn = http.client.HTTPSConnection(redirectLocation.netloc)
            self.api_conn.request("PUT", url, payload, headers)
            response = self.api_conn.getresponse()
            LOGGER.debug('Response status: {}'.format(response.status))
            if response.status != 200:
                LOGGER.error("sendChange: Redirect with non 200 response")

        rsp_data = json.loads(response.read().decode("utf-8"))
        LOGGER.debug('API Response: {}'.format(json.dumps(rsp_data)))

    drivers = [ { 'driver': 'ST', 'value': 0, 'uom': 2 } ]
    commands = {'DISCOVER': discover}
    id = 'NEST_CTR'


if __name__ == "__main__":
    try:
        polyglot = polyinterface.Interface('Nest2')
        polyglot.start()
        control = Controller(polyglot)
        control.runForever()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
