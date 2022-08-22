"""
Test common configuration loading.
"""

from unittest import TestCase

from django.test.utils import override_settings

from edx_event_bus_kafka import config

try:
    from confluent_kafka.schema_registry import SchemaRegistryClient
except ImportError:  # pragma: no cover
    SchemaRegistryClient = None  # pragma: no cover


class TestSchemaRegistryClient(TestCase):
    def test_unconfigured(self):
        assert config.create_schema_registry_client() is None

    def test_configured(self):
        with override_settings(EVENT_BUS_KAFKA_SCHEMA_REGISTRY_URL='http://localhost:12345'):
            assert isinstance(config.create_schema_registry_client(), SchemaRegistryClient)


class TestCommonSettings(TestCase):
    def test_unconfigured(self):
        assert config.load_common_settings() is None

    def test_minimal(self):
        with override_settings(
                EVENT_BUS_KAFKA_BOOTSTRAP_SERVERS='http://localhost:54321',
        ):
            assert config.load_common_settings() == {
                'bootstrap.servers': 'http://localhost:54321',
            }

    def test_full(self):
        with override_settings(
                EVENT_BUS_KAFKA_BOOTSTRAP_SERVERS='http://localhost:54321',
                EVENT_BUS_KAFKA_API_KEY='some_other_key',
                EVENT_BUS_KAFKA_API_SECRET='some_other_secret',
        ):
            assert config.load_common_settings() == {
                'bootstrap.servers': 'http://localhost:54321',
                'sasl.mechanism': 'PLAIN',
                'security.protocol': 'SASL_SSL',
                'sasl.username': 'some_other_key',
                'sasl.password': 'some_other_secret',
            }
