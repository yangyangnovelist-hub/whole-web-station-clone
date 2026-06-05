import { model } from "@medusajs/framework/utils"

const ProjectDraft = model.define("project_draft", {
  id: model.id().primaryKey(),
  customer_id: model.text().nullable(),
  session_id: model.text().nullable(),
  source: model.text().default("website"),
  status: model.enum(["ACTIVE", "SUBMITTED", "ARCHIVED"]).default("ACTIVE"),
  contact_name: model.text().nullable(),
  email: model.text().nullable(),
  company: model.text().nullable(),
  phone: model.text().nullable(),
  notes: model.text().nullable(),
  items_payload: model.text().default("[]"),
  submitted_at: model.dateTime().nullable(),
})

export default ProjectDraft
