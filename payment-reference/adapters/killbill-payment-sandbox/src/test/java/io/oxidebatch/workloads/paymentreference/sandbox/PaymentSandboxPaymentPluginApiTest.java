package io.oxidebatch.workloads.paymentreference.sandbox;

import java.io.InputStream;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.Statement;
import java.util.Collections;
import java.util.UUID;

import org.h2.jdbcx.JdbcDataSource;
import org.joda.time.DateTime;
import org.joda.time.DateTimeZone;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.killbill.billing.catalog.api.Currency;
import org.killbill.billing.payment.plugin.api.PaymentPluginApiException;
import org.killbill.billing.payment.plugin.api.PaymentPluginStatus;
import org.killbill.billing.payment.plugin.api.PaymentTransactionInfoPlugin;
import org.killbill.billing.util.callcontext.CallContext;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PaymentSandboxPaymentPluginApiTest {
    private UUID tenantId;
    private PaymentSandboxDao dao;
    private FakeClient client;
    private PaymentSandboxPaymentPluginApi api;
    private CallContext context;

    @BeforeEach
    void setUp() throws Exception {
        JdbcDataSource dataSource = new JdbcDataSource();
        dataSource.setURL("jdbc:h2:mem:payment_plugin_" + UUID.randomUUID() + ";MODE=MySQL;DB_CLOSE_DELAY=-1");
        applyDdl(dataSource);
        dao = new PaymentSandboxDao(dataSource);
        client = new FakeClient();
        api = new PaymentSandboxPaymentPluginApi(
                new KillBillPaymentAdapter(client),
                dao,
                () -> new DateTime(1_700_000_000_000L, DateTimeZone.UTC));
        tenantId = UUID.randomUUID();
        context = context(tenantId);
    }

    @Test
    void authorizePersistsProviderIdentityAndShortCircuitsCompletedReplay() throws Exception {
        UUID accountId = UUID.randomUUID();
        UUID paymentId = UUID.randomUUID();
        UUID transactionId = UUID.randomUUID();

        PaymentTransactionInfoPlugin first = api.authorizePayment(
                accountId, paymentId, transactionId, UUID.randomUUID(),
                new BigDecimal("12.34"), Currency.USD, Collections.emptyList(), context);
        PaymentTransactionInfoPlugin replay = api.authorizePayment(
                accountId, paymentId, transactionId, UUID.randomUUID(),
                new BigDecimal("12.34"), Currency.USD, Collections.emptyList(), context);

        assertEquals(PaymentPluginStatus.PROCESSED, first.getStatus());
        assertEquals(first.getFirstPaymentReferenceId(), replay.getFirstPaymentReferenceId());
        assertEquals(1, client.createCalls);
        assertEquals(1, client.authorizeCalls);

        PaymentSandboxTransactionRecord record = dao.findByTransaction(transactionId, tenantId);
        assertNotNull(record);
        assertEquals(KillBillPaymentAdapter.providerPaymentId(paymentId), record.getProviderPaymentId());
        assertEquals(KillBillPaymentAdapter.createIdempotencyKey(paymentId), record.getProviderCreateIdempotencyKey());
        assertEquals(KillBillPaymentAdapter.operationIdempotencyKey(transactionId, "authorize"), record.getProviderIdempotencyKey());
        assertFalse(record.isAmbiguous());
    }

    @Test
    void conflictingReplayIsRejectedBeforeAnotherProviderMutation() throws Exception {
        UUID accountId = UUID.randomUUID();
        UUID paymentId = UUID.randomUUID();
        UUID transactionId = UUID.randomUUID();

        api.authorizePayment(
                accountId, paymentId, transactionId, UUID.randomUUID(),
                new BigDecimal("12.34"), Currency.USD, Collections.emptyList(), context);

        assertThrows(PaymentPluginApiException.class, () -> api.authorizePayment(
                accountId, paymentId, transactionId, UUID.randomUUID(),
                new BigDecimal("99.99"), Currency.USD, Collections.emptyList(), context));
        assertEquals(1, client.authorizeCalls);
    }

    @Test
    void ambiguousMutationBecomesUndefinedAndRetainsReplayIdentity() throws Exception {
        client.ambiguousAuthorize = true;
        UUID accountId = UUID.randomUUID();
        UUID paymentId = UUID.randomUUID();
        UUID transactionId = UUID.randomUUID();

        PaymentTransactionInfoPlugin info = api.authorizePayment(
                accountId, paymentId, transactionId, UUID.randomUUID(),
                new BigDecimal("10.00"), Currency.USD, Collections.emptyList(), context);

        assertEquals(PaymentPluginStatus.UNDEFINED, info.getStatus());
        PaymentSandboxTransactionRecord record = dao.findByTransaction(transactionId, tenantId);
        assertTrue(record.isAmbiguous());
        assertTrue(record.isRetryable());
        assertEquals("transport_error", record.getGatewayErrorCode());
        assertEquals(KillBillPaymentAdapter.operationIdempotencyKey(transactionId, "authorize"),
                record.getProviderIdempotencyKey());
    }

    @Test
    void purchaseRetainsBothProviderMutationIdentities() throws Exception {
        UUID accountId = UUID.randomUUID();
        UUID paymentId = UUID.randomUUID();
        UUID transactionId = UUID.randomUUID();

        PaymentTransactionInfoPlugin info = api.purchasePayment(
                accountId, paymentId, transactionId, UUID.randomUUID(),
                new BigDecimal("5.00"), Currency.USD, Collections.emptyList(), context);

        assertEquals(PaymentPluginStatus.PROCESSED, info.getStatus());
        PaymentSandboxTransactionRecord record = dao.findByTransaction(transactionId, tenantId);
        assertEquals(KillBillPaymentAdapter.operationIdempotencyKey(transactionId, "purchase-authorize"),
                record.getProviderIdempotencyKey());
        assertEquals(KillBillPaymentAdapter.operationIdempotencyKey(transactionId, "purchase-capture"),
                record.getProviderSecondaryIdempotencyKey());
    }

    private static CallContext context(UUID tenantId) {
        return (CallContext) Proxy.newProxyInstance(
                CallContext.class.getClassLoader(),
                new Class<?>[]{CallContext.class},
                (proxy, method, args) -> {
                    if ("getTenantId".equals(method.getName())) return tenantId;
                    if (method.getReturnType() == boolean.class) return false;
                    if (method.getReturnType() == int.class) return 0;
                    if (method.getReturnType() == long.class) return 0L;
                    return null;
                });
    }

    private static void applyDdl(JdbcDataSource dataSource) throws Exception {
        try (InputStream input = PaymentSandboxPaymentPluginApiTest.class.getResourceAsStream("/ddl.sql")) {
            String ddl = new String(input.readAllBytes(), StandardCharsets.UTF_8);
            try (Connection connection = dataSource.getConnection();
                 Statement statement = connection.createStatement()) {
                for (String sql : ddl.split(";")) {
                    if (!sql.trim().isEmpty()) statement.execute(sql);
                }
            }
        }
    }

    private static final class FakeClient implements PaymentSandboxClient {
        private int createCalls;
        private int authorizeCalls;
        private boolean ambiguousAuthorize;
        private PaymentSandboxPayment current;

        @Override
        public PaymentSandboxPayment createPayment(String id, long amount, String currency,
                                                   String idempotencyKey, String correlationId) {
            createCalls++;
            current = payment(id, amount, currency, "pending", 0, 0, 0, 1);
            return current;
        }

        @Override
        public PaymentSandboxPayment authorize(String id, String idempotencyKey, String correlationId) {
            authorizeCalls++;
            if (ambiguousAuthorize) {
                throw PaymentSandboxException.transport(
                        "response lost", true, id, idempotencyKey, new java.io.IOException("lost"));
            }
            current = payment(id, current.getAmount(), current.getCurrency(), "authorized",
                    current.getAmount(), 0, 0, 2);
            return current;
        }

        @Override
        public PaymentSandboxPayment capture(String id, long amount, String currency,
                                             String idempotencyKey, String correlationId) {
            current = payment(id, current.getAmount(), currency, "captured",
                    current.getAmount(), current.getAmount(), 0, 3);
            return current;
        }

        @Override
        public PaymentSandboxPayment cancel(String id, String idempotencyKey, String correlationId) {
            current = payment(id, current.getAmount(), current.getCurrency(), "cancelled",
                    current.getAmount(), 0, 0, 3);
            return current;
        }

        @Override
        public PaymentSandboxPayment refund(String id, long amount, String currency,
                                            String idempotencyKey, String correlationId) {
            current = payment(id, current.getAmount(), currency, "refunded",
                    current.getAmount(), current.getAmount(), current.getAmount(), 4);
            return current;
        }

        @Override
        public PaymentSandboxPayment getPayment(String id, String correlationId) {
            return current;
        }

        private static PaymentSandboxPayment payment(String id, long amount, String currency, String status,
                                                     long authorized, long captured, long refunded, long version) {
            return new PaymentSandboxPayment(id, amount, currency, status, authorized, captured, refunded, version);
        }
    }
}
