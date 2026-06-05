import { Migration } from "@medusajs/framework/mikro-orm/migrations"

export class Migration20260419093500 extends Migration {
  override async up(): Promise<void> {
    this.addSql(
      `create table if not exists "project_draft" ("id" text not null, "customer_id" text null, "session_id" text null, "source" text not null default 'website', "status" text check ("status" in ('ACTIVE', 'SUBMITTED', 'ARCHIVED')) not null default 'ACTIVE', "contact_name" text null, "email" text null, "company" text null, "phone" text null, "notes" text null, "items_payload" text not null default '[]', "submitted_at" timestamptz null, "created_at" timestamptz not null default now(), "updated_at" timestamptz not null default now(), "deleted_at" timestamptz null, constraint "project_draft_pkey" primary key ("id"));`
    )
    this.addSql(
      `CREATE INDEX IF NOT EXISTS "IDX_project_draft_deleted_at" ON "project_draft" ("deleted_at") WHERE deleted_at IS NULL;`
    )
  }

  override async down(): Promise<void> {
    this.addSql(`drop table if exists "project_draft" cascade;`)
  }
}
