import { SubscriberArgs, SubscriberConfig } from "@medusajs/framework"
import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import { buildLeadProfile } from "../lib/ai/lead-profile"
import {
  renderQuoteEmail,
  renderRequestReceivedEmail,
  renderSalesLeadEmail,
} from "../lib/email/templates"
import { sendEmail } from "../lib/email/send"
import { LEAD_CREATED_EVENT } from "../lib/events"
import type { QuoteResult } from "../lib/instant-quote/engine"
import { crawlCompanySite } from "../lib/lead-intel/crawl"
import { summarizeTrail, TrailSummary } from "../lib/lead-intel/trail"
import {
  packoasisConfig,
  resumeQuoteUrl,
  unsubscribeUrl,
} from "../lib/packoasis-config"
import { LEAD_MODULE } from "../modules/lead"
import type LeadModuleService from "../modules/lead/service"
import { RFQ_MODULE } from "../modules/rfq"

/**
 * Lead automation for every new RFQ:
 * 1. email the buyer right away (instant quote with checkout link, or an
 *    acknowledgement for reviewed requests),
 * 2. join the anonymous browsing trail and read the company website,
 * 3. build an AI (or heuristic) lead profile and score,
 * 4. alert sales with everything in one email.
 */
export default async function instantQuoteLeadHandler({
  event: { data },
  container,
}: SubscriberArgs<{ id: string }>) {
  const logger = container.resolve(ContainerRegistrationKeys.LOGGER)
  const rfqService: any = container.resolve(RFQ_MODULE)
  const rfq = await rfqService.retrieveRFQ(data.id)
  const quote = (rfq.quote_payload ?? null) as QuoteResult | null
  const firstName = rfq.contact_name.split(/\s+/)[0] || "there"

  if (quote && rfq.cart_id) {
    const sent = await sendEmail(container, {
      to: rfq.email,
      template: "packoasis-instant-quote",
      idempotencyKey: `quote:${rfq.id}`,
      resourceId: rfq.id,
      resourceType: "rfq",
      email: renderQuoteEmail({
        firstName,
        rfqId: rfq.id,
        quote,
        checkoutUrl: resumeQuoteUrl(rfq.id),
        unsubscribeUrl: unsubscribeUrl(rfq.id),
      }),
      headers: {
        "List-Unsubscribe": `<${unsubscribeUrl(rfq.id)}>`,
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
      },
    })
    if (sent) {
      await rfqService.recordContact(rfq.id)
    }
  } else {
    await sendEmail(container, {
      to: rfq.email,
      template: "packoasis-request-received",
      idempotencyKey: `received:${rfq.id}`,
      resourceId: rfq.id,
      resourceType: "rfq",
      email: renderRequestReceivedEmail({
        firstName,
        rfqId: rfq.id,
        title: rfq.title,
        quote,
      }),
    })
  }

  let trail: TrailSummary | null = null
  if (rfq.visitor_id) {
    try {
      const leadService: LeadModuleService = container.resolve(LEAD_MODULE)
      trail = summarizeTrail(await leadService.listVisitorTrail(rfq.visitor_id))
    } catch (error) {
      logger.warn(`[lead] trail lookup failed: ${(error as Error).message}`)
    }
  }

  const site = packoasisConfig.enrichmentEnabled()
    ? await crawlCompanySite({ website: rfq.website, email: rfq.email }, logger)
    : null

  const profile = await buildLeadProfile(
    {
      contact_name: rfq.contact_name,
      email: rfq.email,
      company: rfq.company,
      phone: rfq.phone,
      notes: rfq.notes,
      quote: quote
        ? {
            summary: quote.summary,
            total: quote.total,
            currency_code: quote.currency_code,
            rush: quote.specs.rush,
            instant: quote.instant,
          }
        : null,
      trail,
      site,
    },
    logger
  )

  await rfqService.updateRFQS({
    id: rfq.id,
    lead_score: profile.lead_score,
    lead_grade: profile.lead_grade,
    enriched_at: new Date(),
    enrichment_payload: {
      profile,
      trail,
      site: site
        ? {
            url: site.url,
            title: site.title,
            description: site.description,
            site_name: site.site_name,
            social_links: site.social_links,
            organization: site.organization,
          }
        : null,
    },
  })

  try {
    const analytics = container.resolve(Modules.ANALYTICS)
    await analytics.track({
      event: "lead_scored",
      actor_id: rfq.email,
      properties: {
        rfq_id: rfq.id,
        source: rfq.source,
        lead_score: profile.lead_score,
        lead_grade: profile.lead_grade,
        quoted_total: rfq.quoted_total,
      },
    })
  } catch (error) {
    logger.warn(`[lead] analytics track failed: ${(error as Error).message}`)
  }

  await sendEmail(container, {
    to: packoasisConfig.salesEmails(),
    template: "packoasis-sales-lead",
    idempotencyKey: `sales-lead:${rfq.id}`,
    resourceId: rfq.id,
    resourceType: "rfq",
    email: renderSalesLeadEmail({
      rfq,
      quote,
      profile,
      trail,
      adminUrl: `${packoasisConfig.backendUrl()}/app/leads?rfq=${rfq.id}`,
    }),
  })

  logger.info(
    `[lead] ${rfq.id} processed: grade ${profile.lead_grade} (${profile.lead_score}, ${profile.source})`
  )
}

export const config: SubscriberConfig = {
  event: LEAD_CREATED_EVENT,
}
