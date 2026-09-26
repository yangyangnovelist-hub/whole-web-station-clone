import type { LeadProfile } from "../ai/lead-profile"
import type { QuoteResult } from "../instant-quote/engine"
import type { TrailSummary } from "../lead-intel/trail"

export type RenderedEmail = { subject: string; html: string; text: string }

const BRAND = "#1a7f45"
const ACCENT = "#40C173"
const INK = "#1f2430"
const MUTED = "#606577"

export function escapeHtml(value: unknown) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
}

export function money(amount: number, currency = "usd") {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency.toUpperCase(),
    minimumFractionDigits: 2,
    maximumFractionDigits: amount < 1 ? 4 : 2,
  }).format(amount)
}

function layout(title: string, body: string, footer = "") {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>${escapeHtml(
    title
  )}</title></head><body style="margin:0;padding:0;background:#f4f6f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:${INK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f5;padding:24px 0;"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;border-top:4px solid ${ACCENT};">
<tr><td style="padding:24px 32px 8px;font-size:20px;font-weight:700;color:${INK};">PackOasis</td></tr>
<tr><td style="padding:8px 32px 24px;font-size:15px;line-height:1.6;">${body}</td></tr>
<tr><td style="padding:16px 32px 24px;font-size:12px;line-height:1.5;color:${MUTED};border-top:1px solid #e6e9e7;">${
    footer || "PackOasis custom packaging &middot; hello@packoasis.com"
  }</td></tr>
</table></td></tr></table></body></html>`
}

function button(label: string, href: string) {
  return `<table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0;"><tr><td style="background:${BRAND};border-radius:8px;"><a href="${escapeHtml(
    href
  )}" style="display:inline-block;padding:13px 24px;color:#ffffff;font-weight:600;text-decoration:none;font-size:15px;">${escapeHtml(
    label
  )}</a></td></tr></table>`
}

function quoteTable(quote: QuoteResult) {
  const rows: [string, string][] = [
    ["Product", quote.product_label],
    ["Size", quote.spec_labels.dimensions],
    ["Material", quote.spec_labels.material],
    ["Print", quote.spec_labels.print],
  ]
  if (quote.spec_labels.finishes.length) {
    rows.push(["Finishes", quote.spec_labels.finishes.join(", ")])
  }
  if (quote.spec_labels.addons.length) {
    rows.push(["Add-ons", quote.spec_labels.addons.join(", ")])
  }
  rows.push(
    ["Quantity", `${quote.quantity.toLocaleString("en-US")} pcs`],
    ["Unit price", money(quote.unit_price, quote.currency_code)],
    [
      "Production",
      `${quote.lead_time.production_days[0]}-${quote.lead_time.production_days[1]} business days${
        quote.lead_time.rush ? " (rush)" : ""
      }`,
    ],
    [
      "Estimated delivery",
      `${quote.estimated_delivery.from} to ${quote.estimated_delivery.to}`,
    ]
  )

  return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:16px 0;font-size:14px;">
${rows
  .map(
    ([label, value]) =>
      `<tr><td style="padding:8px 0;color:${MUTED};border-bottom:1px solid #eef0ef;width:40%;">${escapeHtml(
        label
      )}</td><td style="padding:8px 0;border-bottom:1px solid #eef0ef;font-weight:500;">${escapeHtml(
        value
      )}</td></tr>`
  )
  .join("\n")}
<tr><td style="padding:12px 0;font-weight:700;">Total${
    quote.freight_included ? " (freight included)" : ""
  }</td><td style="padding:12px 0;font-weight:700;font-size:18px;">${escapeHtml(
    money(quote.total, quote.currency_code)
  )}</td></tr></table>`
}

function tiersTable(quote: QuoteResult) {
  const larger = quote.tiers.filter((tier) => tier.quantity > quote.quantity)
  if (!larger.length) {
    return ""
  }
  return `<p style="margin:16px 0 4px;font-weight:600;">Order more, pay less per piece</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;">${larger
    .map(
      (tier) =>
        `<tr><td style="padding:4px 16px 4px 0;">${tier.quantity.toLocaleString(
          "en-US"
        )} pcs</td><td style="padding:4px 16px 4px 0;">${escapeHtml(
          money(tier.unit_price, quote.currency_code)
        )} each</td><td style="padding:4px 0;color:${BRAND};">save ${tier.savings_pct}%</td></tr>`
    )
    .join("")}</table>`
}

function unsubscribeFooter(url: string) {
  return `You received this because you requested a quote on packoasis.com. <a href="${escapeHtml(
    url
  )}" style="color:${MUTED};">Stop quote reminders</a>.`
}

function quoteText(quote: QuoteResult) {
  return [
    `${quote.summary}`,
    `Unit price: ${money(quote.unit_price, quote.currency_code)}`,
    `Total${quote.freight_included ? " (freight included)" : ""}: ${money(quote.total, quote.currency_code)}`,
    `Production: ${quote.lead_time.production_days.join("-")} business days`,
    `Estimated delivery: ${quote.estimated_delivery.from} to ${quote.estimated_delivery.to}`,
    `Quote valid until ${quote.valid_until}`,
  ].join("\n")
}

export function renderQuoteEmail(input: {
  firstName: string
  rfqId: string
  quote: QuoteResult
  checkoutUrl: string
  unsubscribeUrl: string
}): RenderedEmail {
  const { quote } = input
  const ref = input.rfqId.slice(-8).toUpperCase()
  const subject = `Your PackOasis quote ${ref}: ${money(quote.total, quote.currency_code)} for ${quote.quantity.toLocaleString(
    "en-US"
  )} ${quote.product_label.replace(/^Custom /, "").toLowerCase()}`

  const html = layout(
    subject,
    `<p>Hi ${escapeHtml(input.firstName)},</p>
<p>Here is your instant quote. Prices are locked until <strong>${escapeHtml(
      quote.valid_until
    )}</strong>, and you can place the order in about two minutes.</p>
${quoteTable(quote)}
${button("Complete my order", input.checkoutUrl)}
${tiersTable(quote)}
<p style="font-size:13px;color:${MUTED};">${quote.assumptions.map(escapeHtml).join("<br>")}</p>
<p>Questions about artwork, sizing or samples? Just reply to this email and a packaging specialist will help.</p>
<p style="color:${MUTED};font-size:13px;">Quote reference: ${escapeHtml(ref)}</p>`,
    unsubscribeFooter(input.unsubscribeUrl)
  )

  const text = `Hi ${input.firstName},\n\nHere is your PackOasis instant quote (ref ${ref}).\n\n${quoteText(
    quote
  )}\n\nComplete your order: ${input.checkoutUrl}\n\nReply to this email with any questions.\n\nStop quote reminders: ${input.unsubscribeUrl}`

  return { subject, html, text }
}

export function renderRequestReceivedEmail(input: {
  firstName: string
  rfqId: string
  title: string
  quote: QuoteResult | null
}): RenderedEmail {
  const ref = input.rfqId.slice(-8).toUpperCase()
  const subject = `We received your PackOasis request ${ref}`
  const html = layout(
    subject,
    `<p>Hi ${escapeHtml(input.firstName)},</p>
<p>Thanks for your request for <strong>${escapeHtml(input.title)}</strong>. A packaging specialist is reviewing it and will send a confirmed quote within one business day.</p>
${
  input.quote
    ? `<p>Indicative budget based on your specs:</p>${quoteTable(input.quote)}<p style="font-size:13px;color:${MUTED};">${input.quote.assumptions
        .map(escapeHtml)
        .join("<br>")}</p>`
    : ""
}
<p>Reply to this email to add artwork, reference photos or deadlines.</p>
<p style="color:${MUTED};font-size:13px;">Request reference: ${escapeHtml(ref)}</p>`
  )
  const text = `Hi ${input.firstName},\n\nThanks for your request (${ref}) for ${input.title}. A PackOasis specialist will send a confirmed quote within one business day.${
    input.quote ? `\n\nIndicative budget:\n${quoteText(input.quote)}` : ""
  }\n\nReply to this email to add artwork or deadlines.`

  return { subject, html, text }
}

export function renderFollowupEmail(input: {
  firstName: string
  subject: string
  paragraphs: string[]
  quote: QuoteResult
  checkoutUrl: string
  unsubscribeUrl: string
}): RenderedEmail {
  const html = layout(
    input.subject,
    `<p>Hi ${escapeHtml(input.firstName)},</p>
${input.paragraphs.map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join("\n")}
${quoteTable(input.quote)}
${button("Review and order", input.checkoutUrl)}
<p>Best,<br>The PackOasis team</p>`,
    unsubscribeFooter(input.unsubscribeUrl)
  )
  const text = `Hi ${input.firstName},\n\n${input.paragraphs.join("\n\n")}\n\n${quoteText(
    input.quote
  )}\n\nReview and order: ${input.checkoutUrl}\n\nBest,\nThe PackOasis team\n\nStop quote reminders: ${input.unsubscribeUrl}`

  return { subject: input.subject, html, text }
}

export function renderSalesLeadEmail(input: {
  rfq: {
    id: string
    contact_name: string
    email: string
    company?: string | null
    phone?: string | null
    website?: string | null
    source: string
    notes?: string | null
  }
  quote: QuoteResult | null
  profile: LeadProfile | null
  trail: TrailSummary | null
  adminUrl: string
}): RenderedEmail {
  const { rfq, quote, profile, trail } = input
  const grade = profile
    ? `${profile.lead_grade} / ${profile.lead_score}`
    : "n/a"
  const subject = `[Lead ${profile?.lead_grade ?? "?"}] ${rfq.company || rfq.contact_name}: ${
    quote
      ? `${money(quote.total, quote.currency_code)} ${quote.product_label}`
      : "new request"
  }`

  const facts: [string, string][] = [
    ["Contact", `${rfq.contact_name} <${rfq.email}>`],
    ["Company", rfq.company || profile?.company_name || "-"],
    ["Phone", rfq.phone || "-"],
    ["Website", rfq.website || "-"],
    ["Source", rfq.source],
    ["Score", grade],
  ]

  const html = layout(
    subject,
    `<p style="font-weight:600;">New ${escapeHtml(rfq.source.replace(/_/g, " "))} lead</p>
<table role="presentation" width="100%" style="font-size:14px;border-collapse:collapse;">${facts
      .map(
        ([label, value]) =>
          `<tr><td style="padding:4px 0;color:${MUTED};width:30%;">${escapeHtml(
            label
          )}</td><td style="padding:4px 0;">${escapeHtml(value)}</td></tr>`
      )
      .join("")}</table>
${quote ? quoteTable(quote) : ""}
${
  profile
    ? `<p style="font-weight:600;margin-top:16px;">AI lead profile (${escapeHtml(profile.source)})</p>
<p>${escapeHtml(profile.company_summary)}</p>
<p><strong>Industry:</strong> ${escapeHtml(profile.industry ?? "unknown")} &middot; <strong>Size:</strong> ${escapeHtml(
        profile.company_size_estimate
      )}</p>
<p><strong>Likely needs:</strong> ${escapeHtml(profile.likely_packaging_needs.join(", ") || "-")}</p>
<p><strong>Why this score:</strong> ${escapeHtml(profile.score_reasons.join("; ") || "-")}</p>
<p><strong>Next best action:</strong> ${escapeHtml(profile.next_best_action)}</p>`
    : ""
}
${
  trail && trail.events
    ? `<p style="font-weight:600;margin-top:16px;">Browsing trail</p>
<p>${trail.pages_viewed} page views, ${trail.quote_interactions} quote interactions, ~${trail.active_minutes} active minutes${
        trail.entry_referrer
          ? `, came from ${escapeHtml(trail.entry_referrer)}`
          : ""
      }.</p>
<ul>${trail.top_pages
        .map(
          (page) =>
            `<li>${escapeHtml(page.title || page.path)} (${page.views}x)</li>`
        )
        .join("")}</ul>`
    : ""
}
${rfq.notes ? `<p><strong>Notes:</strong> ${escapeHtml(rfq.notes)}</p>` : ""}
${button("Open in admin", input.adminUrl)}`
  )

  const text = `${subject}\n\n${facts.map(([label, value]) => `${label}: ${value}`).join("\n")}\n\n${
    quote ? quoteText(quote) : ""
  }\n\n${profile ? `AI profile: ${profile.company_summary}\nNext: ${profile.next_best_action}` : ""}\n\nAdmin: ${input.adminUrl}`

  return { subject, html, text }
}

export function renderOrderConfirmationEmail(input: {
  firstName: string
  displayId: string | number
  items: {
    title: string
    subtitle?: string | null
    quantity: number
    total: number
  }[]
  total: number
  currencyCode: string
}): RenderedEmail {
  const subject = `PackOasis order #${input.displayId} confirmed`
  const html = layout(
    subject,
    `<p>Hi ${escapeHtml(input.firstName)},</p>
<p>Thank you for your order. Next, a packaging specialist will send your digital proof (usually within one business day); production starts as soon as you approve it.</p>
<table role="presentation" width="100%" style="border-collapse:collapse;font-size:14px;margin:16px 0;">${input.items
      .map(
        (item) =>
          `<tr><td style="padding:8px 0;border-bottom:1px solid #eef0ef;">${escapeHtml(item.title)}${
            item.subtitle
              ? `<br><span style="color:${MUTED};font-size:13px;">${escapeHtml(item.subtitle)}</span>`
              : ""
          }</td><td style="padding:8px 0;border-bottom:1px solid #eef0ef;text-align:right;">${escapeHtml(
            money(item.total, input.currencyCode)
          )}</td></tr>`
      )
      .join("")}
<tr><td style="padding:12px 0;font-weight:700;">Total</td><td style="padding:12px 0;text-align:right;font-weight:700;">${escapeHtml(
      money(input.total, input.currencyCode)
    )}</td></tr></table>
<p>Reply to this email any time to reach your packaging specialist.</p>`
  )
  const text = `Hi ${input.firstName},\n\nThank you for your PackOasis order #${input.displayId}. A specialist will send your digital proof within one business day.\n\n${input.items
    .map((item) => `${item.title}: ${money(item.total, input.currencyCode)}`)
    .join("\n")}\nTotal: ${money(input.total, input.currencyCode)}`

  return { subject, html, text }
}

export function renderSalesOrderEmail(input: {
  displayId: string | number
  email: string
  total: number
  currencyCode: string
  rfqId?: string | null
  minutesFromQuote?: number | null
  adminUrl: string
}): RenderedEmail {
  const subject = `[Order] #${input.displayId} ${money(input.total, input.currencyCode)} from ${input.email}`
  const html = layout(
    subject,
    `<p>New order <strong>#${escapeHtml(input.displayId)}</strong> for <strong>${escapeHtml(
      money(input.total, input.currencyCode)
    )}</strong> from ${escapeHtml(input.email)}.</p>
${
  input.rfqId
    ? `<p>Instant quote ${escapeHtml(input.rfqId)}${
        input.minutesFromQuote != null
          ? `, ordered ${input.minutesFromQuote} minutes after the quote`
          : ""
      }.</p>`
    : ""
}
<p>Next: send the digital proof and confirm the delivery address.</p>
${button("Open order in admin", input.adminUrl)}`
  )
  return { subject, html, text: `${subject}\n\nAdmin: ${input.adminUrl}` }
}
