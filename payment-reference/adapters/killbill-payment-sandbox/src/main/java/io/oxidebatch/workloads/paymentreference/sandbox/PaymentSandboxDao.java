package io.oxidebatch.workloads.paymentreference.sandbox;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.UUID;

import javax.sql.DataSource;

import org.joda.time.DateTime;
import org.joda.time.DateTimeZone;
import org.killbill.billing.catalog.api.Currency;
import org.killbill.billing.payment.api.TransactionType;
import org.killbill.billing.payment.plugin.api.PaymentPluginStatus;

public final class PaymentSandboxDao {
    private static final String TABLE = "payment_sandbox_transactions";
    private final DataSource dataSource;

    public PaymentSandboxDao(DataSource dataSource) {
        this.dataSource = Objects.requireNonNull(dataSource, "dataSource");
    }

    public void save(PaymentSandboxTransactionRecord record) throws SQLException {
        try (Connection connection = dataSource.getConnection()) {
            int updated = update(connection, record);
            if (updated == 0) {
                try {
                    insert(connection, record);
                } catch (SQLException e) {
                    if (update(connection, record) == 0) {
                        throw e;
                    }
                }
            }
        }
    }

    public PaymentSandboxTransactionRecord findByTransaction(UUID kbTransactionId, UUID kbTenantId) throws SQLException {
        String sql = "select * from " + TABLE + " where kb_payment_transaction_id = ? and kb_tenant_id = ?";
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, kbTransactionId.toString());
            statement.setString(2, kbTenantId.toString());
            try (ResultSet rs = statement.executeQuery()) {
                return rs.next() ? read(rs) : null;
            }
        }
    }

    public List<PaymentSandboxTransactionRecord> findByPayment(UUID kbPaymentId, UUID kbTenantId) throws SQLException {
        String sql = "select * from " + TABLE + " where kb_payment_id = ? and kb_tenant_id = ? order by record_id asc";
        try (Connection connection = dataSource.getConnection();
             PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, kbPaymentId.toString());
            statement.setString(2, kbTenantId.toString());
            try (ResultSet rs = statement.executeQuery()) {
                List<PaymentSandboxTransactionRecord> records = new ArrayList<>();
                while (rs.next()) {
                    records.add(read(rs));
                }
                return records;
            }
        }
    }

    private int update(Connection connection, PaymentSandboxTransactionRecord record) throws SQLException {
        String sql = "update " + TABLE + " set plugin_status=?, provider_payment_id=?, provider_create_idempotency_key=?, "
                + "provider_idempotency_key=?, provider_secondary_idempotency_key=?, provider_status=?, provider_version=?, "
                + "gateway_error=?, gateway_error_code=?, retryable=?, ambiguous=?, updated_date=? "
                + "where kb_payment_transaction_id=? and kb_tenant_id=?";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            int i = 1;
            statement.setString(i++, record.getPluginStatus().name());
            statement.setString(i++, record.getProviderPaymentId());
            statement.setString(i++, record.getProviderCreateIdempotencyKey());
            statement.setString(i++, record.getProviderIdempotencyKey());
            statement.setString(i++, record.getProviderSecondaryIdempotencyKey());
            statement.setString(i++, record.getProviderStatus());
            statement.setLong(i++, record.getProviderVersion());
            statement.setString(i++, record.getGatewayError());
            statement.setString(i++, record.getGatewayErrorCode());
            statement.setInt(i++, record.isRetryable() ? 1 : 0);
            statement.setInt(i++, record.isAmbiguous() ? 1 : 0);
            statement.setTimestamp(i++, timestamp(record.getUpdatedDate()));
            statement.setString(i++, record.getKbTransactionId().toString());
            statement.setString(i, record.getKbTenantId().toString());
            return statement.executeUpdate();
        }
    }

    private void insert(Connection connection, PaymentSandboxTransactionRecord record) throws SQLException {
        String sql = "insert into " + TABLE
                + " (kb_account_id,kb_payment_id,kb_payment_transaction_id,transaction_type,amount,currency,"
                + "plugin_status,provider_payment_id,provider_create_idempotency_key,provider_idempotency_key,"
                + "provider_secondary_idempotency_key,provider_status,provider_version,gateway_error,gateway_error_code,"
                + "retryable,ambiguous,created_date,updated_date,kb_tenant_id) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            int i = 1;
            statement.setString(i++, record.getKbAccountId().toString());
            statement.setString(i++, record.getKbPaymentId().toString());
            statement.setString(i++, record.getKbTransactionId().toString());
            statement.setString(i++, record.getTransactionType().name());
            statement.setBigDecimal(i++, record.getAmount());
            statement.setString(i++, record.getCurrency() == null ? null : record.getCurrency().name());
            statement.setString(i++, record.getPluginStatus().name());
            statement.setString(i++, record.getProviderPaymentId());
            statement.setString(i++, record.getProviderCreateIdempotencyKey());
            statement.setString(i++, record.getProviderIdempotencyKey());
            statement.setString(i++, record.getProviderSecondaryIdempotencyKey());
            statement.setString(i++, record.getProviderStatus());
            statement.setLong(i++, record.getProviderVersion());
            statement.setString(i++, record.getGatewayError());
            statement.setString(i++, record.getGatewayErrorCode());
            statement.setInt(i++, record.isRetryable() ? 1 : 0);
            statement.setInt(i++, record.isAmbiguous() ? 1 : 0);
            statement.setTimestamp(i++, timestamp(record.getCreatedDate()));
            statement.setTimestamp(i++, timestamp(record.getUpdatedDate()));
            statement.setString(i, record.getKbTenantId().toString());
            statement.executeUpdate();
        }
    }

    private static PaymentSandboxTransactionRecord read(ResultSet rs) throws SQLException {
        String currency = rs.getString("currency");
        return new PaymentSandboxTransactionRecord(
                UUID.fromString(rs.getString("kb_account_id")),
                UUID.fromString(rs.getString("kb_payment_id")),
                UUID.fromString(rs.getString("kb_payment_transaction_id")),
                TransactionType.valueOf(rs.getString("transaction_type")),
                rs.getBigDecimal("amount"),
                currency == null ? null : Currency.valueOf(currency),
                PaymentPluginStatus.valueOf(rs.getString("plugin_status")),
                rs.getString("provider_payment_id"),
                rs.getString("provider_create_idempotency_key"),
                rs.getString("provider_idempotency_key"),
                rs.getString("provider_secondary_idempotency_key"),
                rs.getString("provider_status"),
                rs.getLong("provider_version"),
                rs.getString("gateway_error"),
                rs.getString("gateway_error_code"),
                rs.getInt("retryable") != 0,
                rs.getInt("ambiguous") != 0,
                dateTime(rs.getTimestamp("created_date")),
                dateTime(rs.getTimestamp("updated_date")),
                UUID.fromString(rs.getString("kb_tenant_id")));
    }

    private static Timestamp timestamp(DateTime value) {
        return new Timestamp(value.getMillis());
    }

    private static DateTime dateTime(Timestamp value) {
        return new DateTime(value.getTime(), DateTimeZone.UTC);
    }
}
