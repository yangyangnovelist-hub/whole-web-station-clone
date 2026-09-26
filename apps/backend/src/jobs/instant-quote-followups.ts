import { MedusaContainer } from "@medusajs/framework/types"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { writeFollowupCopy } from "../lib/ai/followup-copy"
import { renderFollowupEmail } from "../lib/email/templates"
import { sendEmail } from "../lib/email/send"
import type { QuoteResult } from "../lib/instant-quote/engine"
import {
  packoasisConfig,
  resumeQuoteUrl,
  unsubscribeUrl,
} from "../lib/packoasis-config"
import { LEAD_MODULE } from "../modules/lead"
import type LeadModuleService from "../modules/lead/service"
import { RFQ_MODULE } from "../modules/rfq"

const HOUR = 60 * 60 * 1000
const MIN_SPACING = 12 * HOUR

/**
 * Follows up on instant quotes that were not ordered (24h and 72h after the
 * quote by default), then prunes old browsing events.
 */
export default async function instantQuoteFollowups(
  container: MedusaContainer
) {
  const logger = container.resolve(ContainerRegistrationKeys.LOGGER)
  const query = container.resolve(ContainerRegistrationKeys.QUERY)
  const rfqService: any = container.resolve(RFQ_MODULE)

  if (packoasisConfig.followupsEnabled()) {
    const delays = packoasisConfig.followupDelaysHours()
    const candidates = await rfqService.listRFQS(
      {
        source: "instant_quote",
        status: "QUOTED",
        contact_opt_out: false,
        order_id: null,
      },
      { take: 200, order: { created_at: "ASC" } }
    )

    const now = Date.now()
    for (const rfq of candidates) {
      const attempt = (rfq.followup_count ?? 0) + 1
      const quote = rfq.quote_payload as QuoteResult | null
      if (!quote || attempt > delays.length) {
        continue
      }
      const due =
        new Date(rfq.created_at).getTime() + delays[attempt - 1] * HOUR
      const lastContact = rfq.last_contacted_at
        ? new Date(rfq.last_contacted_at).getTime()
        : 0
      if (now < due || now - lastContact < MIN_SPACING) {
        continue
      }

      if (rfq.cart_id) {
        const {
          data: [cart],
        } = await query.graph({
          entity: "cart",
          fields: ["id", "completed_at"],
          filters: { id: rfq.cart_id },
        })
        if (!cart || cart.completed_at) {
          continue
        }
      }

      const profile = (
        rfq.enrichment_payload as { profile?: Record<string, any> } | null
      )?.profile
      const firstName = rfq.contact_name.split(/\s+/)[0] || "there"
      const copy = await writeFollowupCopy(
        {
          attempt,
          first_name: firstName,
          company: rfq.company,
          quote_summary: quote.summary,
          lead_time: `${quote.lead_time.production_days[0]}-${quote.lead_time.production_days[1]} business days`,
          personalization_hook: profile?.personalization_hook ?? null,
          industry: profile?.industry ?? null,
        },
        logger
      )

      const sent = await sendEmail(container, {
        to: rfq.email,
        template: `packoasis-quote-followup-${attempt}`,
        idempotencyKey: `followup:${rfq.id}:${attempt}`,
        resourceId: rfq.id,
        resourceType: "rfq",
        email: renderFollowupEmail({
          firstName,
          subject: copy.subject,
          paragraphs: copy.paragraphs,
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
        await rfqService.recordContact(rfq.id, { followup: true })
        logger.info(
          `[followups] sent follow-up ${attempt} for ${rfq.id} (${copy.source})`
        )
      }
    }
  }

  const retentionDays = packoasisConfig.leadEventRetentionDays()
  if (retentionDays > 0) {
    const leadService: LeadModuleService = container.resolve(LEAD_MODULE)
    const cutoff = new Date(Date.now() - retentionDays * 24 * HOUR)
    const expired = await leadService.listLeadEvents(
      { created_at: { $lt: cutoff } },
      { select: ["id"], take: 1000 }
    )
    if (expired.length) {
      await leadService.deleteLeadEvents(expired.map((event) => event.id))
      logger.info(
        `[followups] pruned ${expired.length} lead events older than ${retentionDays} days`
      )
    }
  }
}

export const config = {
  name: "packoasis-instant-quote-followups",
  schedule: "*/30 * * * *",
}
