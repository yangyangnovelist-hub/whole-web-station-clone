import { Migration } from "@medusajs/framework/mikro-orm/migrations"

export class Migration20260926091000 extends Migration {
  override async up(): Promise<void> {
    this.addSql(
      `create table if not exists "lead_event" ("id" text not null, "visitor_id" text not null, "type" text not null, "url" text null, "path" text null, "title" text null, "referrer" text null, "product_type" text null, "payload" jsonb null, "created_at" timestamptz not null default now(), "updated_at" timestamptz not null default now(), "deleted_at" timestamptz null, constraint "lead_event_pkey" primary key ("id"));`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_lead_event_deleted_at" ON "lead_event" ("deleted_at") WHERE deleted_at IS NULL;`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_lead_event_visitor_id" ON "lead_event" ("visitor_id") WHERE deleted_at IS NULL;`
    )
  }

  override async down(): Promise<void> {
    this.addSql(`drop table if exists "lead_event" cascade;`)
  }
}
