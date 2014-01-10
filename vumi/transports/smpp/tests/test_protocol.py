from twisted.test import proto_helpers
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, Deferred, returnValue
from twisted.internet.error import ConnectionDone
from twisted.internet.task import Clock


from vumi.tests.helpers import VumiTestCase, PersistenceHelper
from vumi.transports.smpp.transport import SmppTransport
from vumi.transports.smpp.protocol import (
    EsmeTransceiver, EsmeTransceiverFactory,
    EsmeTransmitterFactory, EsmeReceiverFactory)
from vumi.transports.smpp.pdu_utils import (
    seq_no, command_status, command_id, chop_pdu_stream)
from vumi.transports.smpp.smpp_utils import unpacked_pdu_opts
from vumi.transports.smpp.clientserver.sequence import RedisSequence
from vumi.config import ConfigError

from smpp.pdu import unpack_pdu
from smpp.pdu_builder import (
    Unbind, UnbindResp,
    BindTransceiver, BindTransceiverResp,
    BindTransmitter, BindTransmitterResp,
    BindReceiver, BindReceiverResp,
    SubmitSMResp,
    DeliverSM,
    EnquireLink, EnquireLinkResp)


def connect_transport(protocol):
    transport = proto_helpers.StringTransport()
    protocol.makeConnection(transport)
    d = protocol.bind(system_id='', password='', system_type='')
    d.addCallback(lambda _: transport)
    return d


@inlineCallbacks
def bind_protocol(transport, protocol, clear=True):
    [bind_pdu] = yield wait_for_pdus(transport, 1)
    resp_pdu_class = {
        BindTransceiver: BindTransceiverResp,
        BindReceiver: BindReceiverResp,
        BindTransmitter: BindTransmitterResp,
    }.get(protocol.bind_pdu)
    protocol.dataReceived(
        resp_pdu_class(seq_no(bind_pdu)).get_bin())
    [enquire_link] = yield wait_for_pdus(transport, 1)
    protocol.dataReceived(
        EnquireLinkResp(seq_no(enquire_link)).get_bin())
    if clear:
        transport.clear()
    returnValue(bind_pdu)


def wait_for_pdus(transport, count):
    d = Deferred()

    def cb(pdus):
        data_stream = transport.value()
        pdu_found = chop_pdu_stream(data_stream)
        if pdu_found is not None:
            pdu_data, remainder = pdu_found
            pdu = unpack_pdu(pdu_data)
            pdus.append(pdu)
            transport.clear()
            transport.write(remainder)

        if len(pdus) == count:
            d.callback(pdus)
        else:
            reactor.callLater(0, cb, pdus)

    cb([])

    return d


class DummySmppTransport(object):
    pass


class EsmeTestCase(VumiTestCase):

    @inlineCallbacks
    def setUp(self):
        self.persistence_helper = self.add_helper(PersistenceHelper())
        self.redis = yield self.persistence_helper.get_redis_manager()
        self.clock = Clock()
        self.patch(EsmeTransceiver, 'clock', self.clock)

    def get_protocol(self, config={},
                     sm_processor=None, dr_processor=None,
                     factory_class=None):

        factory_class = factory_class or EsmeTransceiverFactory

        default_config = {
            'transport_name': 'sphex_transport',
            'twisted_endpoint': 'tcp:host=localhost:port=0',
            'short_message_processor': ('vumi.transports.smpp.processors.'
                                        'DeliverShortMessageProcessor'),
            'delivery_report_processor': ('vumi.transports.smpp.processors.'
                                          'DeliveryReportProcessor'),
            'smpp_config': {
                'system_id': 'system_id',
                'password': 'password',
                'smpp_bind_timeout': 30,
            }
        }
        default_config['smpp_config'].update(config)

        dummy_smpp_transport = DummySmppTransport()
        dummy_smpp_transport.get_static_config = lambda: cfg
        dummy_smpp_transport.redis = self.redis

        cfg = SmppTransport.CONFIG_CLASS(default_config, static=True)
        if sm_processor is None:
            sm_processor = cfg.short_message_processor(
                dummy_smpp_transport, cfg.short_message_processor_config)
        if dr_processor is None:
            dr_processor = cfg.delivery_report_processor(
                dummy_smpp_transport, cfg.delivery_report_processor_config)

        dummy_smpp_transport.dr_processor = dr_processor
        dummy_smpp_transport.sm_processor = sm_processor
        dummy_smpp_transport.sequence_generator = RedisSequence(self.redis)

        factory = factory_class(dummy_smpp_transport)
        proto = factory.buildProtocol(('127.0.0.1', 0))
        self.add_cleanup(proto.connectionLost, reason=ConnectionDone)
        return proto

    def assertCommand(self, pdu, cmd_id, sequence_number=None,
                      status=None, params={}):
        self.assertEqual(command_id(pdu), cmd_id)
        if sequence_number is not None:
            self.assertEqual(seq_no(pdu), sequence_number)
        if status is not None:
            self.assertEqual(command_status(pdu), status)

        if params:
            if 'body' not in pdu:
                raise Exception('Body does not have parameters.')

            mandatory_parameters = pdu['body']['mandatory_parameters']
            for key, value in params.items():
                self.assertEqual(mandatory_parameters.get(key), value)

    @inlineCallbacks
    def setup_bind(self, config={}, clear=True, factory_class=None):
        protocol = self.get_protocol(config, factory_class=factory_class)
        transport = yield connect_transport(protocol)
        yield bind_protocol(transport, protocol, clear=clear)
        returnValue((transport, protocol))

    def test_on_connection_made(self):
        protocol = self.get_protocol()
        self.assertEqual(protocol.state, EsmeTransceiver.CLOSED_STATE)
        transport = yield connect_transport(protocol)
        self.assertEqual(protocol.state, EsmeTransceiver.OPEN_STATE)
        [bind_pdu] = yield wait_for_pdus(transport, 1)
        self.assertCommand(
            bind_pdu,
            'bind_transceiver',
            sequence_number=1,
            params={
                'system_id': 'system_id',
                'password': 'password',
            })

    def test_drop_link(self):
        protocol = self.get_protocol()
        transport = yield connect_transport(protocol)
        self.assertFalse(protocol.isBound())
        self.assertEqual(protocol.state, EsmeTransceiver.OPEN_STATE)
        self.assertFalse(transport.disconnecting)
        self.clock.advance(protocol.config.smpp_bind_timeout + 1)
        self.assertTrue(transport.disconnecting)

    @inlineCallbacks
    def test_on_smpp_bind(self):
        protocol = self.get_protocol()
        transport = yield connect_transport(protocol)
        yield bind_protocol(transport, protocol)
        self.assertEqual(protocol.state, EsmeTransceiver.BOUND_STATE_TRX)
        self.assertTrue(protocol.isBound())
        self.assertTrue(protocol.enquire_link_call.running)

    @inlineCallbacks
    def test_handle_unbind(self):
        transport, protocol = yield self.setup_bind()
        protocol.dataReceived(Unbind(sequence_number=0).get_bin())
        [pdu] = yield wait_for_pdus(transport, 1)
        self.assertCommand(pdu, 'unbind_resp',
                           sequence_number=0, status='ESME_ROK')

    @inlineCallbacks
    def test_on_submit_sm_resp(self):
        calls = []
        self.patch(EsmeTransceiver, 'onSubmitSMResp',
                   lambda p, *a: calls.append(a))
        transport, protocol = yield self.setup_bind()
        pdu = SubmitSMResp(sequence_number=0, message_id='foo')
        protocol.dataReceived(pdu.get_bin())
        self.assertEqual(calls, [(0, 'foo', 'ESME_ROK')])

    @inlineCallbacks
    def test_deliver_sm(self):
        calls = []
        self.patch(EsmeTransceiver, 'onDeliverSM',
                   lambda p, *a: calls.append(a))
        transport, protocol = yield self.setup_bind()
        pdu = DeliverSM(
            sequence_number=0, message_id='foo', short_message='bar')
        protocol.dataReceived(pdu.get_bin())
        [(seq_no, deliver_sm)] = calls
        self.assertEqual(seq_no, 0)
        self.assertCommand(deliver_sm, 'deliver_sm', sequence_number=0)

        [deliver_sm_resp] = yield wait_for_pdus(transport, 1)
        self.assertCommand(
            deliver_sm_resp, 'deliver_sm_resp', sequence_number=0,
            status='ESME_ROK')

    @inlineCallbacks
    def test_deliver_sm_fail(self):
        self.patch(EsmeTransceiver, 'onDeliverSM',
                   lambda p, *a: 'ESME_RDELIVERYFAILURE')
        transport, protocol = yield self.setup_bind()
        pdu = DeliverSM(
            sequence_number=0, message_id='foo', short_message='bar')
        protocol.dataReceived(pdu.get_bin())
        [deliver_sm_resp] = yield wait_for_pdus(transport, 1)
        self.assertCommand(
            deliver_sm_resp, 'deliver_sm_resp', sequence_number=0,
            status='ESME_RDELIVERYFAILURE')

    @inlineCallbacks
    def test_on_enquire_link(self):
        transport, protocol = yield self.setup_bind()
        pdu = EnquireLink(sequence_number=0)
        protocol.dataReceived(pdu.get_bin())
        [enquire_link_resp] = yield wait_for_pdus(transport, 1)
        self.assertCommand(
            enquire_link_resp, 'enquire_link_resp', sequence_number=0,
            status='ESME_ROK')

    @inlineCallbacks
    def test_on_enquire_link_resp(self):
        calls = []
        self.patch(EsmeTransceiver, 'onEnquireLinkResp',
                   lambda p, *a: calls.append(a))
        transport, protocol = yield self.setup_bind()
        [(seq_number,)] = calls
        # bind_transceiver is sequence_number 1
        self.assertEqual(seq_number, 2)

    @inlineCallbacks
    def test_enquire_link_no_response(self):
        transport, protocol = yield self.setup_bind(clear=False)
        interval = protocol.config.smpp_enquire_link_interval
        protocol.clock.advance(interval)
        self.assertTrue(transport.disconnecting)

    @inlineCallbacks
    def test_enquire_link_looping(self):
        transport, protocol = yield self.setup_bind(clear=False)
        interval = protocol.config.smpp_enquire_link_interval
        enquire_link_resp = EnquireLinkResp(1)

        protocol.clock.advance(interval - 1)
        protocol.dataReceived(enquire_link_resp.get_bin())

        protocol.clock.advance(interval - 1)
        self.assertFalse(transport.disconnecting)
        protocol.clock.advance(1)
        self.assertTrue(transport.disconnecting)

    @inlineCallbacks
    def test_submit_sm(self):
        transport, protocol = yield self.setup_bind()
        yield protocol.submitSM(short_message='foo')
        [submit_sm] = yield wait_for_pdus(transport, 1)
        self.assertCommand(submit_sm, 'submit_sm', params={
            'short_message': 'foo',
        })

    @inlineCallbacks
    def test_submit_sm_long(self):
        transport, protocol = yield self.setup_bind(config={
            'send_long_messages': True,
        })

        long_message = 'This is a long message.' * 20
        yield protocol.submitSM(short_message=long_message)
        [submit_sm] = yield wait_for_pdus(transport, 1)
        pdu_opts = unpacked_pdu_opts(submit_sm)

        self.assertEqual('submit_sm', submit_sm['header']['command_id'])
        self.assertEqual(
            None, submit_sm['body']['mandatory_parameters']['short_message'])
        self.assertEqual(''.join('%02x' % ord(c) for c in long_message),
                         pdu_opts['message_payload'])

    @inlineCallbacks
    def test_submit_sm_multipart_udh(self):
        transport, protocol = yield self.setup_bind(config={
            'send_multipart_udh': True,
        })
        long_message = 'This is a long message.' * 20
        seq_numbers = yield protocol.submitSM(short_message=long_message)
        pdus = yield wait_for_pdus(transport, 4)
        self.assertEqual(len(seq_numbers), 4)

        msg_parts = []
        msg_refs = []

        for i, sm in enumerate(pdus):
            mandatory_parameters = sm['body']['mandatory_parameters']
            self.assertEqual('submit_sm', sm['header']['command_id'])
            msg = mandatory_parameters['short_message']

            udh_hlen, udh_tag, udh_len, udh_ref, udh_tot, udh_seq = [
                ord(octet) for octet in msg[:6]]
            self.assertEqual(5, udh_hlen)
            self.assertEqual(0, udh_tag)
            self.assertEqual(3, udh_len)
            msg_refs.append(udh_ref)
            self.assertEqual(4, udh_tot)
            self.assertEqual(i + 1, udh_seq)
            self.assertTrue(len(msg) <= 136)
            msg_parts.append(msg[6:])
            self.assertEqual(0x40, mandatory_parameters['esm_class'])

        self.assertEqual(long_message, ''.join(msg_parts))
        self.assertEqual(1, len(set(msg_refs)))

    @inlineCallbacks
    def test_submit_sm_multipart_sar(self):
        transport, protocol = yield self.setup_bind(config={
            'send_multipart_sar': True,
        })
        long_message = 'This is a long message.' * 20
        seq_nums = yield protocol.submitSM(short_message=long_message)
        pdus = yield wait_for_pdus(transport, 4)
        # seq no 1 == bind_transceiver, 2 == enquire_link
        self.assertEqual([3, 4, 5, 6], seq_nums)
        msg_parts = []
        msg_refs = []

        for i, sm in enumerate(pdus):
            pdu_opts = unpacked_pdu_opts(sm)
            mandatory_parameters = sm['body']['mandatory_parameters']

            self.assertEqual('submit_sm', sm['header']['command_id'])
            msg_parts.append(mandatory_parameters['short_message'])
            self.assertTrue(len(mandatory_parameters['short_message']) <= 130)
            msg_refs.append(pdu_opts['sar_msg_ref_num'])
            self.assertEqual(i + 1, pdu_opts['sar_segment_seqnum'])
            self.assertEqual(4, pdu_opts['sar_total_segments'])

        self.assertEqual(long_message, ''.join(msg_parts))
        self.assertEqual(1, len(set(msg_refs)))

    @inlineCallbacks
    def test_query_sm(self):
        transport, protocol = yield self.setup_bind()
        yield protocol.querySM('foo', 'bar')
        [query_sm] = yield wait_for_pdus(transport, 1)
        self.assertCommand(query_sm, 'query_sm', params={
            'message_id': 'foo',
            'source_addr': 'bar',
        })

    @inlineCallbacks
    def test_unbind(self):
        calls = []
        self.patch(EsmeTransceiver, 'onUnbindResp',
                   lambda p, sn: calls.append(sn))
        transport, protocol = yield self.setup_bind()
        yield protocol.unbind()
        [unbind_pdu] = yield wait_for_pdus(transport, 1)
        protocol.dataReceived(UnbindResp(seq_no(unbind_pdu)).get_bin())
        self.assertEqual(calls, [seq_no(unbind_pdu)])

    @inlineCallbacks
    def test_bind_transmitter(self):
        transport, protocol = yield self.setup_bind(
            factory_class=EsmeTransmitterFactory)
        self.assertTrue(protocol.isBound())
        self.assertEqual(protocol.state, protocol.BOUND_STATE_TX)

    @inlineCallbacks
    def test_bind_receiver(self):
        transport, protocol = yield self.setup_bind(
            factory_class=EsmeReceiverFactory)
        self.assertTrue(protocol.isBound())
        self.assertEqual(protocol.state, protocol.BOUND_STATE_RX)


class TestSmppTransportConfig(VumiTestCase):

    def required_config(self, config_params):
        config = {
            "system_id": "vumitest-vumitest-vumitest",
            "password": "password",
        }
        config.update(config_params)
        return config

    def get_config(self, config_dict):
        return EsmeTransceiver.CONFIG_CLASS(config_dict)

    def assert_config_error(self, config_dict):
        try:
            self.get_config(config_dict)
            self.fail("ConfigError not raised.")
        except ConfigError as err:
            return err.args[0]

    def test_long_message_params(self):
        self.get_config(self.required_config({}))
        self.get_config(self.required_config({'send_long_messages': True}))
        self.get_config(self.required_config({'send_multipart_sar': True}))
        self.get_config(self.required_config({'send_multipart_udh': True}))
        errmsg = self.assert_config_error(self.required_config({
            'send_long_messages': True,
            'send_multipart_sar': True,
        }))
        self.assertEqual(errmsg, (
            "The following parameters are mutually exclusive: "
            "send_long_messages, send_multipart_sar"))
        errmsg = self.assert_config_error(self.required_config({
            'send_long_messages': True,
            'send_multipart_sar': True,
            'send_multipart_udh': True,
        }))
        self.assertEqual(errmsg, (
            "The following parameters are mutually exclusive: "
            "send_long_messages, send_multipart_sar, send_multipart_udh"))
