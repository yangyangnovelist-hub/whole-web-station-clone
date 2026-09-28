import { model } from "@medusajs/framework/utils"

/**
 * First-party browsing / funnel events keyed by an anonymous visitor id that
 * the quote widget keeps in localStorage. No IP addresses are stored.
 */
const LeadEvent = model
  .define("lead_event", {
    id: model.id({ prefix: "levt" }).primaryKey(),
    visitor_id: model.text(),
    type: model.text(),
    url: model.text().nullable(),
    path: model.text().nullable(),
    title: model.text().nullable(),
    referrer: model.text().nullable(),
    product_type: model.text().nullable(),
    payload: model.json().nullable(),
  })
  .indexes([{ on: ["visitor_id"], where: "deleted_at IS NULL" }])

export default LeadEvent
