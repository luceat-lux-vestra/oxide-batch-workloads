package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	paymentapplication "proxynth/payment-sandbox/internal/payment/application"
	paymentsqlite "proxynth/payment-sandbox/internal/payment/adapters/sqlite"
	paymentdomain "proxynth/payment-sandbox/internal/payment/domain"
	"proxynth/payment-sandbox/internal/platform/config"
	"proxynth/payment-sandbox/internal/platform/observability"
	persistencesqlite "proxynth/payment-sandbox/internal/platform/persistence/sqlite"
)

const businessVirtualTimeKey = "business_virtual_time"

type eventOnlyPublisher struct {
	events     paymentapplication.EventLog
	occurredAt time.Time
}

func (p *eventOnlyPublisher) Publish(
	ctx context.Context,
	payment *paymentdomain.Payment,
	eventType paymentdomain.EventType,
) error {
	if payment == nil {
		return errors.New("payment event cannot be published for a nil payment")
	}
	metadata := observability.MetadataFromContext(ctx)
	if strings.TrimSpace(metadata.CorrelationID) == "" {
		return errors.New("campaign control requires an explicit correlation id")
	}

	previous, err := p.events.ListByAggregate(ctx, payment.ID())
	if err != nil {
		return err
	}
	causationID := paymentdomain.EventID("")
	var causationVersion uint64
	for _, candidate := range previous {
		if candidate.AggregateVersion() < payment.Version() &&
			candidate.AggregateVersion() > causationVersion {
			causationID = candidate.ID()
			causationVersion = candidate.AggregateVersion()
		}
	}

	payload, err := json.Marshal(paymentdomain.NewEventSnapshot(payment))
	if err != nil {
		return fmt.Errorf("encode payment event payload: %w", err)
	}
	eventID := paymentdomain.EventID(
		fmt.Sprintf("%s:%s:%d", payment.ID(), eventType, payment.Version()),
	)
	event, err := paymentdomain.NewBusinessEventWithPayload(
		eventID,
		payment.ID(),
		eventType,
		p.occurredAt.UTC(),
		payment.Version(),
		metadata.CorrelationID,
		causationID,
		payload,
	)
	if err != nil {
		return err
	}
	return p.events.Append(ctx, event)
}

func printable(value string) bool {
	for _, r := range value {
		if r < 0x20 || r == 0x7f {
			return false
		}
	}
	return true
}

func run(args []string) error {
	if len(args) == 0 || args[0] != "fail" {
		return errors.New("usage: payment-sandbox-campaign-control fail --payment-id <id> --correlation-id <id>")
	}

	flags := flag.NewFlagSet("fail", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	paymentID := flags.String("payment-id", "", "existing provider payment id")
	correlationID := flags.String("correlation-id", "", "deterministic campaign correlation id")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	if strings.TrimSpace(*paymentID) == "" || !printable(*paymentID) {
		return errors.New("payment id must be a non-blank printable value")
	}
	if strings.TrimSpace(*correlationID) == "" || len(*correlationID) > 128 || !printable(*correlationID) {
		return errors.New("correlation id must be a printable value of at most 128 characters")
	}

	ctx := context.Background()
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load payment-sandbox configuration: %w", err)
	}
	db, err := persistencesqlite.Open(ctx, cfg.Database)
	if err != nil {
		return fmt.Errorf("open payment-sandbox database: %w", err)
	}
	defer func() { _ = db.Close() }()

	at, exists, err := persistencesqlite.NewRuntimeStateStore(db).Load(
		ctx,
		businessVirtualTimeKey,
	)
	if err != nil {
		return fmt.Errorf("load payment-sandbox business time: %w", err)
	}
	if !exists {
		return errors.New("payment-sandbox business virtual time is not initialized")
	}

	repository := paymentsqlite.NewRepository(db)
	publisher := &eventOnlyPublisher{
		events:     paymentsqlite.NewEventLogRepository(db),
		occurredAt: at,
	}
	command := paymentapplication.NewFailPaymentWithPublisher(repository, publisher)
	ctx = observability.WithMetadata(ctx, observability.Metadata{
		CorrelationID: *correlationID,
	})
	if _, err := command.Execute(ctx, paymentapplication.FailPaymentCommand{
		PaymentID: paymentdomain.ID(*paymentID),
	}); err != nil {
		return fmt.Errorf("fail provider payment: %w", err)
	}

	payment, err := repository.FindByID(ctx, paymentdomain.ID(*paymentID))
	if err != nil {
		return fmt.Errorf("read failed provider payment: %w", err)
	}
	if payment.Status() != paymentdomain.StatusFailed {
		return fmt.Errorf("provider payment did not reach failed status: %s", payment.Status())
	}

	return json.NewEncoder(os.Stdout).Encode(map[string]any{
		"payment_id": payment.ID(),
		"status":     payment.Status(),
		"version":    payment.Version(),
	})
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		_ = json.NewEncoder(os.Stderr).Encode(map[string]string{
			"decision": "BLOCK",
			"error":    err.Error(),
		})
		os.Exit(75)
	}
}
