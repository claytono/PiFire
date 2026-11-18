'''
*****************************************
PiFire Probes Home Assistant Module
*****************************************

Description:
  This module reads temperature sensors from Home Assistant and presents them as
  PiFire probes.  Each port is assigned a Home Assistant entity ID, and a
  background thread polls the Home Assistant REST API for those entities using a
  long-lived access token.

  If an entity can't be read (Home Assistant is unreachable, or the entity is
  unavailable or not numeric), its port keeps reporting the last reading.  Ports
  report 0 until their first successful reading.

	Ex Device Definition:

	device = {
		'device' : 'your_device_name',	# Unique name for the device
		'module' : 'homeassistant',		# Must be populated for this module to load properly
		'ports' : ['HA1', 'HA2', 'HA3', 'HA4', 'HA5', 'HA6', 'HA7', 'HA8'],
		'config' : {
			'url' : 'http://homeassistant.local:8123',	# Home Assistant base URL
			'token' : 'your_long_lived_access_token',
			'entity_HA1' : 'sensor.smoker_probe_core_temperature'	# One entry per port used
		}
	}
'''

'''
*****************************************
 Imported Libraries
*****************************************
'''

import logging
import threading
import time
from http import HTTPStatus

import requests

from probes.base import ProbeInterface

PORTS = ['HA1', 'HA2', 'HA3', 'HA4', 'HA5', 'HA6', 'HA7', 'HA8']
POLL_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 5
TEMPERATURE_UNITS = {'°C': 'C', '°F': 'F'}

'''
*****************************************
 Class Definitions
*****************************************
'''

class HomeAssistantDevice:
	'''Polls Home Assistant for the configured entities in a background thread.'''

	def __init__(self, url, token, entities):
		self.logger = logging.getLogger('control')
		self.url = url.rstrip('/')
		self.headers = {'Authorization': f'Bearer {token}'}
		self.entities = entities	# Port -> entity ID
		self.readings = {}			# Port -> (temperature, units) from the last good read
		self.errors = {}			# Entity ID -> reason for the last logged error
		self.connected = False

		self.thread = threading.Thread(target=self._polling_loop, daemon=True)
		self.thread.start()

	def _polling_loop(self):
		while True:
			for port, entity in self.entities.items():
				try:
					reading = self._read_entity(entity)
				except Exception as error:
					reading = self._fail(entity, f'Unexpected error: {error!r}')
				if reading is not None:
					self.readings[port] = reading
			time.sleep(POLL_INTERVAL_SECONDS)

	def _read_entity(self, entity):
		'''Return (temperature, units) for an entity, or None if it can't be read.'''
		try:
			response = requests.get(f'{self.url}/api/states/{entity}', headers=self.headers, timeout=REQUEST_TIMEOUT_SECONDS)
		except requests.RequestException as error:
			self.connected = False
			# Some errors include a different object address on every attempt, so log by error type.
			return self._fail(entity, f'Could not connect to Home Assistant: {error}', type(error).__name__)

		# A rejected token or a server error affects every entity, so treat those as disconnected.
		self.connected = response.status_code not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN) and response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR
		try:
			response.raise_for_status()
			state = response.json()
		except requests.RequestException as error:
			return self._fail(entity, f'Could not read the entity: {error}')

		units = TEMPERATURE_UNITS.get(state['attributes'].get('unit_of_measurement'))
		if units is None:
			return self._fail(entity, f'Unsupported unit of measurement {state["attributes"].get("unit_of_measurement")!r}.')
		try:
			temperature = float(state['state'])
		except ValueError:
			return self._fail(entity, f'State is {state["state"]!r}.')

		if entity in self.errors:
			self.logger.info(f'Home Assistant [{entity}]: Reading normally again.')
			del self.errors[entity]
		return temperature, units

	def _fail(self, entity, message, reason=None):
		'''Log a failure once, until its reason changes.'''
		reason = reason or message
		if self.errors.get(entity) != reason:
			self.logger.error(f'Home Assistant [{entity}]: {message} Keeping the last reading.')
			self.errors[entity] = reason

	def get_status(self):
		return {'connected': self.connected}


class ReadProbes(ProbeInterface, uses_temp_queue=False):

	def _init_device(self):
		self.time_delay = 0
		self.device_info['ports'] = PORTS
		config = self.device_info['config']
		entities = {port: config[f'entity_{port}'] for port in self.port_map if config.get(f'entity_{port}')}
		self.device = HomeAssistantDevice(config['url'], config['token'], entities)

	def update_units(self, units):
		# Avoid ProbeInterface.update_units(), which reinitializes the device and would start a second polling thread.
		self.units = 'C' if units == 'C' else 'F'

	def read_all_ports(self, output_data):
		'''Return the latest reading for each mapped port, or 0 before its first reading.'''
		for port, label in self.port_map.items():
			value = 0
			if port in self.device.readings:
				temperature, units = self.device.readings[port]
				# Like the other probes, report whole degrees in Fahrenheit and tenths in Celsius.
				if self.units == 'F':
					value = int(temperature) if units == 'F' else self._to_fahrenheit(temperature)
				else:
					value = round(temperature if units == 'C' else self._to_celsius(temperature), 1)
			self.output_data['tr'][label] = 0

			if port == self.primary_port:
				self.output_data['primary'][label] = value
			elif port in self.food_ports:
				self.output_data['food'][label] = value
			elif port in self.aux_ports:
				self.output_data['aux'][label] = value

		return self.output_data
