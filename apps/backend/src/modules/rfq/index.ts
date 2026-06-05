import RFQModuleService from "./service"
import { Module } from "@medusajs/framework/utils"

export const RFQ_MODULE = "rfq"

export default Module(RFQ_MODULE, {
  service: RFQModuleService,
})
