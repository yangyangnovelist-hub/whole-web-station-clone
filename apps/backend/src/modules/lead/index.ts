import LeadModuleService from "./service"
import { Module } from "@medusajs/framework/utils"

export const LEAD_MODULE = "lead"

export default Module(LEAD_MODULE, {
  service: LeadModuleService,
})
