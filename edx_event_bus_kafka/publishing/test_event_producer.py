"""
Test the event producer code.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import openedx_events.learning.signals
import pytest
from django.test import override_settings
from openedx_events.event_bus.avro.serializer import AvroSignalSerializer
from openedx_events.learning.data import UserData, UserPersonalData

import edx_event_bus_kafka.publishing.event_producer as ep


class TestEventProducer(TestCase):
    """Test producer."""

    def test_extract_event_key(self):
        event_data = {
            'user': UserData(
                id=123,
                is_active=True,
                pii=UserPersonalData(
                    username='foobob',
                    email='bob@foo.example',
                    name="Bob Foo",
                )
            )
        }

        assert ep.extract_event_key(event_data, 'user.pii.username') == 'foobob'
        with pytest.raises(Exception,
                           match="Could not extract key from event; lookup in xxx failed at 'xxx' in dictionary"):
            ep.extract_event_key(event_data, 'xxx')
        with pytest.raises(Exception,
                           match="Could not extract key from event; lookup in user.xxx failed at 'xxx' in object"):
            ep.extract_event_key(event_data, 'user.xxx')

    def test_descend_avro_schema(self):
        signal = openedx_events.learning.signals.SESSION_LOGIN_COMPLETED
        schema = AvroSignalSerializer(signal).schema

        assert ep.descend_avro_schema(schema, ['user', 'pii', 'username']) == {"name": "username", "type": "string"}

        with pytest.raises(Exception) as excinfo:
            ep.descend_avro_schema(schema, ['user', 'xxx'])
        assert excinfo.value.args == ("Error traversing Avro schema along path ['user', 'xxx']; failed at 'xxx'.",)
        assert isinstance(excinfo.value.__cause__, IndexError)

    def test_extract_key_schema(self):
        signal = openedx_events.learning.signals.SESSION_LOGIN_COMPLETED
        schema = ep.extract_key_schema(AvroSignalSerializer(signal), 'user.pii.username')
        assert schema == '{"name": "username", "type": "string"}'

    def test_get_producer_for_signal(self):
        signal = openedx_events.learning.signals.SESSION_LOGIN_COMPLETED
        with override_settings(
                SCHEMA_REGISTRY_URL='http://localhost:12345',
                SCHEMA_REGISTRY_API_KEY='some_key',
                SCHEMA_REGISTRY_API_SECRET='some_secret',
                KAFKA_BOOTSTRAP_SERVER='http://localhost:54321',
                KAFKA_API_KEY='some_other_key',
                KAFKA_API_SECRET='some_other_secret',
        ):
            producer_first = ep.get_producer_for_signal(signal, 'user.id')
            producer_second = ep.get_producer_for_signal(signal, 'user.id')
            # There's not a lot we can test here, but we can at least
            # check that construction succeeds and that caching is
            # happening.
            assert producer_first is producer_second

    @patch('edx_event_bus_kafka.publishing.event_producer.logger')
    def test_verify_event(self, mock_logger):
        fake_event = MagicMock()
        fake_event.topic.return_value = 'some_topic'
        fake_event.key.return_value = 'some_key'
        fake_event.partition.return_value = 'some_partition'

        ep.verify_event(Exception("problem!"), fake_event)
        mock_logger.warning.assert_called_once_with("Event delivery failed: Exception('problem!')")

        ep.verify_event(None, fake_event)
        mock_logger.info.assert_called_once_with(
            'Event delivered to topic some_topic; key=some_key; partition=some_partition'
        )

    def test_send_to_event_bus(self):
        signal = openedx_events.learning.signals.SESSION_LOGIN_COMPLETED
        event_data = {
            'user': UserData(
                id=123,
                is_active=True,
                pii=UserPersonalData(
                    username='foobob',
                    email='bob@foo.example',
                    name="Bob Foo",
                )
            )
        }

        mock_producer = MagicMock()
        with patch('edx_event_bus_kafka.publishing.event_producer.get_producer_for_signal', return_value=mock_producer):
            ep.send_to_event_bus(signal, 'user_stuff', 'user.id', event_data)

        mock_producer.produce.assert_called_once_with(
            'user_stuff', key=123, value=event_data,
            on_delivery=ep.verify_event,
            headers={'ce_type': 'org.openedx.learning.auth.session.login.completed.v1'},
        )
