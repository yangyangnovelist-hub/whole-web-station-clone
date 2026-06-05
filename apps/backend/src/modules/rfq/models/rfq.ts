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
})

export default RFQ
