"""
Core consumer and event-loop code.
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from edx_django_utils.monitoring import record_exception
from edx_toggles.toggles import SettingToggle
from openedx_events.event_bus.avro.deserializer import AvroSignalDeserializer
from openedx_events.tooling import OpenEdxPublicSignal

from .config import get_full_topic, get_schema_registry_client, load_common_settings

logger = logging.getLogger(__name__)

# See https://github.com/openedx/event-bus-kafka/blob/main/docs/decisions/0005-optional-import-of-confluent-kafka.rst
try:
    import confluent_kafka
    from confluent_kafka import DeserializingConsumer
    from confluent_kafka.schema_registry.avro import AvroDeserializer
except ImportError:  # pragma: no cover
    confluent_kafka = None

# .. toggle_name: EVENT_BUS_KAFKA_CONSUMERS_ENABLED
# .. toggle_implementation: SettingToggle
# .. toggle_default: True
# .. toggle_description: If set to False, consumer will exit immediately. This can be used as an emergency kill-switch
#   to disable a consumer—as long as the management command is killed and restarted when settings change.
# .. toggle_use_cases: opt_out
# .. toggle_creation_date: 2022-01-31
KAFKA_CONSUMERS_ENABLED = SettingToggle('EVENT_BUS_KAFKA_CONSUMERS_ENABLED', default=True)

CONSUMER_POLL_TIMEOUT = getattr(settings, 'EVENT_BUS_KAFKA_CONSUMER_POLL_TIMEOUT', 1.0)

# CloudEvent standard name for the event type header, see
# https://github.com/cloudevents/spec/blob/v1.0.1/kafka-protocol-binding.md#325-example
EVENT_TYPE_HEADER = "ce_type"


class UnusableMessageError(Exception):
    """
    Indicates that a message was successfully received but could not be processed.

    This could be invalid headers, an unknown signal, or other issue specific to
    the contents of the message.
    """


class KafkaEventConsumer:
    """
    Construct consumer for the given topic, group, and signal. The consumer can then
    emit events from the event bus using the configured signal.

    Can also consume messages indefinitely off the queue.
    """

    def __init__(self, topic, group_id, signal):
        if confluent_kafka is None:  # pragma: no cover
            raise Exception('Library confluent-kafka not available. Cannot create event consumer.')

        self.topic = topic
        self.group_id = group_id
        self.signal = signal
        self.consumer = self._create_consumer()
        self._shut_down_loop = False

    # return type (Optional[DeserializingConsumer]) removed from signature to avoid error on import
    def _create_consumer(self):
        """
        Create a DeserializingConsumer for events of the given signal instance.

        Returns
            None if confluent_kafka is not available.
            DeserializingConsumer if it is.
        """

        schema_registry_client = get_schema_registry_client()

        signal_deserializer = AvroSignalDeserializer(self.signal)

        def inner_from_dict(event_data_dict, ctx=None):  # pylint: disable=unused-argument
            return signal_deserializer.from_dict(event_data_dict)

        consumer_config = load_common_settings()

        # We do not deserialize the key because we don't need it for anything yet.
        # Also see https://github.com/openedx/openedx-events/issues/86 for some challenges on determining key schema.
        consumer_config.update({
            'group.id': self.group_id,
            'value.deserializer': AvroDeserializer(schema_str=signal_deserializer.schema_string(),
                                                   schema_registry_client=schema_registry_client,
                                                   from_dict=inner_from_dict),
        })

        return DeserializingConsumer(consumer_config)

    def _shut_down(self):
        """
        Test utility for shutting down the consumer loop.
        """
        self._shut_down_loop = True

    def consume_indefinitely(self):
        """
        Consume events from a topic in an infinite loop.
        """
        # This is already checked at the Command level, but it's possible this loop
        # could get called some other way, so check it here too.
        if not KAFKA_CONSUMERS_ENABLED.is_enabled():
            logger.error("Kafka consumers not enabled, exiting.")
            return

        try:
            full_topic = get_full_topic(self.topic)
            run_context = {
                'full_topic': full_topic,
                'consumer_group': self.group_id,
                'expected_signal': self.signal,
            }
            self.consumer.subscribe([full_topic])
            logger.info(
                f"Running consumer for {run_context!r} with partition assignments {self.consumer.assignment()!r}"
            )

            while True:
                # Allow unit tests to break out of loop
                if self._shut_down_loop:
                    break

                msg = None
                try:
                    msg = self.consumer.poll(timeout=CONSUMER_POLL_TIMEOUT)
                    if msg is not None:
                        self.emit_signals_from_message(msg)
                except BaseException as e:
                    self.record_event_consuming_error(run_context, e, msg)
        finally:
            # Close down consumer to commit final offsets.
            self.consumer.close()
            logger.info("Committing final offsets")

    def emit_signals_from_message(self, msg):
        """
        Determine the correct signal and send the event from the message.

        Arguments:
            msg (Message): Consumed message.
        """
        # DeserializingConsumer.poll() always returns either a valid message
        # or None, and raises an exception in all other cases. This means
        # we don't need to check msg.error() ourselves. But... check it here
        # anyway for robustness against code changes.
        if msg.error() is not None:
            raise Exception(
                f"Polled message had error object (shouldn't happen): {msg.error()!r}"
            )

        headers = msg.headers() or []  # treat None as []

        event_types = [value for key, value in headers if key == EVENT_TYPE_HEADER]
        if len(event_types) == 0:
            raise UnusableMessageError(
                f"Missing {EVENT_TYPE_HEADER} header on message, cannot determine signal"
            )
        if len(event_types) > 1:
            raise UnusableMessageError(
                f"Multiple {EVENT_TYPE_HEADER} headers found on message, cannot determine signal"
            )

        event_type = event_types[0]

        # CloudEvents specifies using UTF-8 for header values, so let's be explicit.
        event_type_str = event_type.decode("utf-8")

        if event_type_str != self.signal.event_type:
            raise UnusableMessageError(
                f"Signal types do not match. Expected {self.signal.event_type}. "
                f"Received message of type {event_type_str}."
            )

        self.signal.send_event(**msg.value())

    def record_event_consuming_error(self, run_context, error, maybe_event):
        """
        Record an error caught while consuming an event, both to the logs and to telemetry.

        Arguments:
            run_context: Dictionary of contextual information: full_topic, consumer_group,
              and expected_signal.
            error: An exception instance
            maybe_event: None if event could not be fetched or decoded, or a Message if one
              was successfully deserialized but could not be processed for some reason
        """
        context_msg = ", ".join(f"{k}={v!r}" for k, v in run_context.items())
        if maybe_event is None:
            event_msg = "no event available"
        else:
            event_details = {
                'partition': maybe_event.partition(),
                'offset': maybe_event.offset(),
                'headers': maybe_event.headers(),
                'key': maybe_event.key(),
                'value': maybe_event.value(),
            }
            event_msg = f"event details: {event_details!r}"

        try:
            # This is gross, but our record_exception wrapper doesn't take args at the moment,
            # and will only read the exception from stack context.
            raise Exception(error)
        except BaseException:
            record_exception()
            logger.exception(
                f"Error consuming event from Kafka: {error!r} in context {context_msg} -- {event_msg}"
            )


class ConsumeEventsCommand(BaseCommand):
    """
    Management command for Kafka consumer workers in the event bus.
    """
    help = """
    Consume messages of specified signal type from a Kafka topic and send their data to that signal.

    example:
        python3 manage.py cms consume_events -t user-login -g user-activity-service \
            -s org.openedx.learning.auth.session.login.completed.v1
    """

    def add_arguments(self, parser):

        parser.add_argument(
            '-t', '--topic',
            nargs=1,
            required=True,
            help='Topic to consume (without environment prefix)'
        )

        parser.add_argument(
            '-g', '--group_id',
            nargs=1,
            required=True,
            help='Consumer group id'
        )
        parser.add_argument(
            '-s', '--signal',
            nargs=1,
            required=True,
            help='Type of signal to emit from consumed messages.'
        )

    def handle(self, *args, **options):
        if not KAFKA_CONSUMERS_ENABLED.is_enabled():
            logger.error("Kafka consumers not enabled, exiting.")
            return
        try:
            signal = OpenEdxPublicSignal.get_signal_by_type(options['signal'][0])
            event_consumer = KafkaEventConsumer(
                topic=options['topic'][0],
                group_id=options['group_id'][0],
                signal=signal,
            )
            event_consumer.consume_indefinitely()
        except Exception:  # pylint: disable=broad-except
            logger.exception("Error consuming Kafka events")
