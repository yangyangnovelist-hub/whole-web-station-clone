import { Migration } from "@medusajs/framework/mikro-orm/migrations"

export class Migration20260926090000 extends Migration {
  override async up(): Promise<void> {
    this.addSql(
      `alter table if exists "rfq" add column if not exists "quote_payload" jsonb null, add column if not exists "quoted_total" real null, add column if not exists "currency_code" text null, add column if not exists "country_code" text null, add column if not exists "cart_id" text null, add column if not exists "order_id" text null, add column if not exists "website" text null, add column if not exists "visitor_id" text null, add column if not exists "lead_score" integer null, add column if not exists "lead_grade" text null, add column if not exists "enrichment_payload" jsonb null, add column if not exists "enriched_at" timestamptz null, add column if not exists "followup_count" integer not null default 0, add column if not exists "last_contacted_at" timestamptz null, add column if not exists "contact_opt_out" boolean not null default false;`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_rfq_cart_id" ON "rfq" ("cart_id") WHERE deleted_at IS NULL;`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_rfq_status_source" ON "rfq" ("status", "source") WHERE deleted_at IS NULL;`
    )
  }

  override async down(): Promise<void> {
    this.addSql(`DROP INDEX IF EXISTS "IDX_rfq_cart_id";`)
    this.addSql(`DROP INDEX IF EXISTS "IDX_rfq_status_source";`)
    this.addSql(
      `alter table if exists "rfq" drop column if exists "quote_payload", drop column if exists "quoted_total", drop column if exists "currency_code", drop column if exists "country_code", drop column if exists "cart_id", drop column if exists "order_id", drop column if exists "website", drop column if exists "visitor_id", drop column if exists "lead_score", drop column if exists "lead_grade", drop column if exists "enrichment_payload", drop column if exists "enriched_at", drop column if exists "followup_count", drop column if exists "last_contacted_at", drop column if exists "contact_opt_out";`
    )
  }
}
