import { model } from "@medusajs/framework/utils"

const RFQ = model.define("rfq", {
  id: model.id().primaryKey(),
  legacy_id: model.number().nullable(),
  buyer_id: model.text().nullable(),
  draft_id: model.text().nullable(),
  source: model.text().default("contact_form"),
  contact_name: model.text(),
  email: model.text(),
  company: model.text().nullable(),
  phone: model.text().nullable(),
  title: model.text(),
  application: model.text().nullable(),
  quantity: model.number(),
  target_price: model.float().nullable(),
  need_by: model.dateTime().nullable(),
  notes: model.text().nullable(),
  items_payload: model.text().nullable(),
  status: model
    .enum([
      "DRAFT",
      "SUBMITTED",
      "QUOTED",
      "COUNTER",
      "ACCEPTED",
      "ORDERED",
      "CLOSED",
    ])
    .default("DRAFT"),
  expires_at: model.dateTime().nullable(),
  // Instant quote → order funnel
  quote_payload: model.json().nullable(),
  quoted_total: model.float().nullable(),
  currency_code: model.text().nullable(),
  country_code: model.text().nullable(),
  cart_id: model.text().nullable(),
  order_id: model.text().nullable(),
  // Lead intelligence (browsing trail, company enrichment, AI scoring)
  website: model.text().nullable(),
  visitor_id: model.text().nullable(),
  lead_score: model.number().nullable(),
  lead_grade: model.text().nullable(),
  enrichment_payload: model.json().nullable(),
  enriched_at: model.dateTime().nullable(),
  // Automated contact
  followup_count: model.number().default(0),
  last_contacted_at: model.dateTime().nullable(),
  contact_opt_out: model.boolean().default(false),
})

export default RFQ
