import { SubscriberArgs, SubscriberConfig } from "@medusajs/framework"
import { Modules } from "@medusajs/framework/utils"

export default async function rfqAnalyticsHandler({
  event: { data },
  container,
}: SubscriberArgs<{ id: string }>) {
  const analyticsModuleService: any = container.resolve(Modules.ANALYTICS)
  const rfqModuleService: any = container.resolve("rfq")

  // Fetch the full RFQ details to get email and title for better tracking
  const rfq = await rfqModuleService.retrieveRFQ(data.id)

  await analyticsModuleService.track({
    event: "rfq_submitted",
    properties: {
      rfq_id: rfq.id,
      email: rfq.email,
      contact_name: rfq.contact_name,
      title: rfq.title,
      source: rfq.source,
      quantity: rfq.quantity,
      company: rfq.company,
    },
  })
}

export const config: SubscriberConfig = {
  event: "rfq.created",
}
