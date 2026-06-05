import { model } from "@medusajs/framework/utils"

const Quote = model.define("quote", {
  id: model.id().primaryKey(),
  legacy_id: model.number().nullable(),
  rfq_id: model.text(),
  vendor_id: model.text(),
  price: model.float(),
  lead_time_days: model.number(),
  notes: model.text().nullable(),
  round: model.number().default(1),
  status: model
    .enum(["PENDING", "ACCEPTED", "REJECTED", "COUNTERED"])
    .default("PENDING"),
})

export default Quote
