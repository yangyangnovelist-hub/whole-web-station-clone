import { Migration } from "@medusajs/framework/mikro-orm/migrations"

export class Migration20260419093000 extends Migration {
  override async up(): Promise<void> {
    this.addSql(
      `create table if not exists "quote" ("id" text not null, "legacy_id" integer null, "rfq_id" text not null, "vendor_id" text not null, "price" real not null, "lead_time_days" integer not null, "notes" text null, "round" integer not null default 1, "status" text check ("status" in ('PENDING', 'ACCEPTED', 'REJECTED', 'COUNTERED')) not null default 'PENDING', "created_at" timestamptz not null default now(), "updated_at" timestamptz not null default now(), "deleted_at" timestamptz null, constraint "quote_pkey" primary key ("id"));`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_quote_deleted_at" ON "quote" ("deleted_at") WHERE deleted_at IS NULL;`
    )

    this.addSql(
      `create table if not exists "rfq" ("id" text not null, "legacy_id" integer null, "buyer_id" text null, "draft_id" text null, "source" text not null default 'contact_form', "contact_name" text not null, "email" text not null, "company" text null, "phone" text null, "title" text not null, "application" text null, "quantity" integer not null, "target_price" real null, "need_by" timestamptz null, "notes" text null, "items_payload" text null, "status" text check ("status" in ('DRAFT', 'SUBMITTED', 'QUOTED', 'COUNTER', 'ACCEPTED', 'ORDERED', 'CLOSED')) not null default 'DRAFT', "expires_at" timestamptz null, "created_at" timestamptz not null default now(), "updated_at" timestamptz not null default now(), "deleted_at" timestamptz null, constraint "rfq_pkey" primary key ("id"));`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_rfq_deleted_at" ON "rfq" ("deleted_at") WHERE deleted_at IS NULL;`
    )
  }

  override async down(): Promise<void> {
    this.addSql(`drop table if exists "quote" cascade;`)
    this.addSql(`drop table if exists "rfq" cascade;`)
  }
}
