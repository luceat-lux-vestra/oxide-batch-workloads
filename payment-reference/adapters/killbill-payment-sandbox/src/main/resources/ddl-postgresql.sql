create table payment_sandbox_transactions (
  record_id bigserial,
  kb_account_id char(36) not null,
  kb_payment_id char(36) not null,
  kb_payment_transaction_id char(36) not null,
  transaction_type varchar(32) not null,
  amount numeric(15,9),
  currency char(3),
  plugin_status varchar(32) not null,
  provider_payment_id varchar(128) not null,
  provider_create_idempotency_key varchar(255) not null,
  provider_idempotency_key varchar(255),
  provider_secondary_idempotency_key varchar(255),
  provider_status varchar(32),
  provider_version bigint not null default 0,
  gateway_error varchar(512),
  gateway_error_code varchar(128),
  retryable smallint not null default 0,
  ambiguous smallint not null default 0,
  created_date timestamp not null,
  updated_date timestamp not null,
  kb_tenant_id char(36) not null,
  primary key(record_id)
);
create unique index payment_sandbox_transactions_kb_tx_tenant
  on payment_sandbox_transactions(kb_payment_transaction_id, kb_tenant_id);
create index payment_sandbox_transactions_kb_payment
  on payment_sandbox_transactions(kb_payment_id, kb_tenant_id);
create index payment_sandbox_transactions_provider_payment
  on payment_sandbox_transactions(provider_payment_id);
