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
/** How long after the last follow-up is due a quote is still tried. */
const GRACE = 7 * 24 * HOUR
const PAGE_SIZE = 200

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

  const delays = packoasisConfig.followupDelaysHours()
  if (packoasisConfig.followupsEnabled() && delays.length) {
    const now = Date.now()
    // Only rows that can still be due, so finished or abandoned quotes never
    // crowd out new ones. All pages are read before any row is updated.
    const filters = {
      source: "instant_quote",
      status: "QUOTED",
      contact_opt_out: false,
      order_id: null,
      cart_id: { $ne: null },
      followup_count: { $lt: delays.length },
      created_at: {
        $gte: new Date(now - Math.max(...delays) * HOUR - GRACE),
        $lte: new Date(now - Math.min(...delays) * HOUR),
      },
    }
    const candidates: any[] = []
    for (let skip = 0; ; skip += PAGE_SIZE) {
      const page = await rfqService.listRFQS(filters, {
        skip,
        take: PAGE_SIZE,
        order: { created_at: "ASC", id: "ASC" },
      })
      candidates.push(...page)
      if (page.length < PAGE_SIZE) {
        break
      }
    }

    for (const rfq of candidates) {
      const attempt = (rfq.followup_count ?? 0) + 1
      const quote = rfq.quote_payload as QuoteResult | null
      // Without a cart the "Review and order" link has nothing to resume.
      if (!quote || !rfq.cart_id || attempt > delays.length) {
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

      // The buyer already ordered a later (e.g. revised) quote.
      const [converted] = await rfqService.listRFQS(
        {
          email: rfq.email,
          order_id: { $ne: null },
          created_at: { $gte: rfq.created_at },
        },
        { select: ["id"], take: 1 }
      )
      if (converted) {
        await rfqService.closeRFQ(rfq.id).catch((error: Error) =>
          logger.warn(
            `[followups] could not close ${rfq.id}: ${error.message}`
          )
        )
        continue
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
      } else {
        // Back off MIN_SPACING before retrying the same attempt.
        await rfqService.recordContact(rfq.id)
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
