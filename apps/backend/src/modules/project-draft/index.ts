import ProjectDraftModuleService from "./service"
import { Module } from "@medusajs/framework/utils"

export const PROJECT_DRAFT_MODULE = "project_draft"

export default Module(PROJECT_DRAFT_MODULE, {
  service: ProjectDraftModuleService,
})
