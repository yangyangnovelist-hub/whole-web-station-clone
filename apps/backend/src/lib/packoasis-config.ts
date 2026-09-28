import crypto from "node:crypto"

/** Runtime settings for the instant-quote / lead automation (all optional). */
export const packoasisConfig = {
  storefrontUrl: () =>
    (process.env.STOREFRONT_URL || "https://packoasis.com").replace(/\/$/, ""),
  backendUrl: () =>
    (process.env.MEDUSA_BACKEND_URL || "http://localhost:9000").replace(
      /\/$/,
      ""
    ),
  emailFrom: () => process.env.PACKOASIS_EMAIL_FROM || undefined,
  replyTo: () => process.env.PACKOASIS_REPLY_TO || "hello@packoasis.com",
  salesEmails: () =>
    (process.env.SALES_NOTIFY_EMAIL || "")
      .split(",")
      .map((email) => email.trim())
      .filter(Boolean),
  defaultCountry: () =>
    (process.env.INSTANT_QUOTE_DEFAULT_COUNTRY || "us").toLowerCase(),
  followupsEnabled: () => process.env.PACKOASIS_FOLLOWUPS_ENABLED !== "false",
  followupDelaysHours: () =>
    (process.env.PACKOASIS_FOLLOWUP_DELAYS_HOURS || "24,72")
      .split(",")
      .map((value) => Number(value.trim()))
      .filter((value) => Number.isFinite(value) && value > 0),
  enrichmentEnabled: () => process.env.PACKOASIS_ENRICHMENT_ENABLED !== "false",
  leadEventRetentionDays: () =>
    Number(process.env.PACKOASIS_LEAD_EVENT_RETENTION_DAYS || 180),
}

function signingSecret() {
  return (
    process.env.PACKOASIS_SIGNING_SECRET ||
    process.env.COOKIE_SECRET ||
    "supersecret"
  )
}

export function signToken(purpose: string, id: string) {
  return crypto
    .createHmac("sha256", signingSecret())
    .update(`${purpose}:${id}`)
    .digest("base64url")
    .slice(0, 32)
}

export function verifyToken(purpose: string, id: string, token: unknown) {
  if (typeof token !== "string" || !token) {
    return false
  }
  const expected = Buffer.from(signToken(purpose, id))
  const actual = Buffer.from(token)
  return (
    expected.length === actual.length &&
    crypto.timingSafeEqual(expected, actual)
  )
}

export function unsubscribeUrl(rfqId: string) {
  return `${packoasisConfig.backendUrl()}/packoasis/unsubscribe?rfq=${encodeURIComponent(
    rfqId
  )}&token=${signToken("unsubscribe", rfqId)}`
}

/**
 * Signed "complete my order" link for emails. It opens a confirmation page on
 * the storefront; pressing "Continue to checkout" there asks the backend
 * server-to-server (POST /packoasis/resume-cart) for a fresh cart for the
 * quote, unless that browser already holds the quote's current cart. Only
 * that browser learns the cart's id (in an HttpOnly cookie), so whoever sent
 * the link cannot read what is entered at checkout, and a mail scanner
 * fetching the link changes nothing.
 */
export function resumeQuoteUrl(rfqId: string, origin?: string) {
  const base = (origin || packoasisConfig.storefrontUrl()).replace(/\/$/, "")
  return `${base}/api/quote-resume?rfq=${encodeURIComponent(
    rfqId
  )}&token=${signToken("resume", rfqId)}`
}

/** Lifetime of a widget checkout link and its po_qn cookie, in seconds. */
export const CHECKOUT_LINK_TTL_SECONDS = 30 * 60

/** The widget's per-request nonce (base64url), kept in its po_qn cookie. */
export const HANDOFF_NONCE = /^[A-Za-z0-9_-]{16,128}$/

function checkoutPayload(cartId: string, handoff: string, exp: string) {
  return `${cartId}:${handoff}:${exp}`
}

/**
 * Widget hand-off that attaches a quote cart to the browser that requested
 * it. The token signs the cart, the widget's po_qn cookie nonce (`handoff`)
 * and an expiry, and the storefront forwards that browser's cookie to
 * /packoasis/checkout-token, so the link only works for 30 minutes in the
 * browser that asked for the quote. Anyone else opening it lacks the cookie.
 */
export function storefrontCheckoutUrl(
  cartId: string,
  countryCode: string,
  handoff: string,
  origin?: string
) {
  const base = (origin || packoasisConfig.storefrontUrl()).replace(/\/$/, "")
  const exp = String(Math.floor(Date.now() / 1000) + CHECKOUT_LINK_TTL_SECONDS)
  return `${base}/api/quote-checkout?cart_id=${encodeURIComponent(
    cartId
  )}&country=${encodeURIComponent(countryCode)}&exp=${exp}&token=${signToken(
    "checkout",
    checkoutPayload(cartId, handoff, exp)
  )}`
}

/**
 * The cart id of an unexpired checkout link signed for this handoff nonce
 * (see storefrontCheckoutUrl), otherwise null.
 */
export function verifyCheckoutLink(input: {
  cart_id?: unknown
  token?: unknown
  exp?: unknown
  handoff?: unknown
}) {
  const { cart_id: cartId, exp, handoff } = input
  if (
    typeof cartId !== "string" ||
    !/^[A-Za-z0-9_]{1,64}$/.test(cartId) ||
    typeof handoff !== "string" ||
    !HANDOFF_NONCE.test(handoff) ||
    typeof exp !== "string" ||
    !/^\d{1,12}$/.test(exp) ||
    Number(exp) * 1000 <= Date.now()
  ) {
    return null
  }
  return verifyToken(
    "checkout",
    checkoutPayload(cartId, handoff, exp),
    input.token
  )
    ? cartId
    : null
}
