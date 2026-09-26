import { ModuleProvider, Modules } from "@medusajs/framework/utils"
import ResendNotificationService from "./service"

export default ModuleProvider(Modules.NOTIFICATION, {
  services: [ResendNotificationService],
})
