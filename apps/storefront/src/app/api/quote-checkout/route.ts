import { NextRequest, NextResponse } from "next/server"

const CART_ID = /^cart_[0-9A-Z]{26}$/
const TOKEN = /^[A-Za-z0-9_-]{1,128}$/
const HANDOFF = /^[A-Za-z0-9_-]{16,128}$/
const EXP = /^\d{1,12}$/
const RFQ_ID = /^[A-Za-z0-9_-]{1,64}$/

const BACKEND_URL = (
  process.env.MEDUSA_BACKEND_URL || "http://localhost:9000"
).replace(/\/$/, "")

type HandoffCheck = { valid: boolean; resumeUrl?: string }

/**
 * Asks the backend whether this link was signed for this cart, this browser's
 * po_qn cookie (set by the quote widget when it requested the quote) and an
 * unexpired `exp`, and whether the cart is still its quote's open cart. A
 * link opened in any other browser, or after 30 minutes, is refused. Any
 * failure counts as invalid.
 */
async function checkHandoff(
  cartId: string,
  token: string,
  exp: string,
  handoff: string
): Promise<HandoffCheck> {
  try {
    const url = new URL(`${BACKEND_URL}/packoasis/checkout-token`)
    url.searchParams.set("cart_id", cartId)
    url.searchParams.set("token", token)
    url.searchParams.set("exp", exp)
    url.searchParams.set("handoff", handoff)
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    })
    if (!response.ok) {
      return { valid: false }
    }
    const body = (await response.json()) as {
      valid?: unknown
      resume_url?: unknown
    }
    return {
      valid: body?.valid === true,
      resumeUrl:
        typeof body?.resume_url === "string" ? body.resume_url : undefined,
    }
  } catch {
    return { valid: false }
  }
}

/**
 * Same-origin /api/quote-resume link for the backend's resume_url, where the
 * buyer can have an expired quote re-priced into a fresh cart.
 */
function resumeRedirect(resumeUrl: string, request: NextRequest) {
  try {
    const params = new URL(resumeUrl).searchParams
    const rfq = params.get("rfq") ?? ""
    const token = params.get("token") ?? ""
    if (!RFQ_ID.test(rfq) || !TOKEN.test(token)) {
      return null
    }
    const target = new URL("/api/quote-resume", request.url)
    target.searchParams.set("rfq", rfq)
    target.searchParams.set("token", token)
    return target
  } catch {
    return null
  }
}

/**
 * Hand-off from the instant quote widget to checkout: the backend already
 * created a cart with the locked quote price, so attach it to the browser
 * that requested the quote and jump straight to the address step. Quote
 * emails use /api/quote-resume instead.
 */
export async function GET(request: NextRequest) {
  const cartId = request.nextUrl.searchParams.get("cart_id") ?? ""
  const token = request.nextUrl.searchParams.get("token") ?? ""
  const exp = request.nextUrl.searchParams.get("exp") ?? ""
  const handoff = request.cookies.get("po_qn")?.value ?? ""
  const country = (
    request.nextUrl.searchParams.get("country") ??
    process.env.NEXT_PUBLIC_DEFAULT_REGION ??
    "us"
  ).toLowerCase()
  const countryCode = /^[a-z]{2}$/.test(country) ? country : "us"
  const contactUs = NextResponse.redirect(
    new URL("/contact-us.html", request.url),
    303
  )

  if (
    !CART_ID.test(cartId) ||
    !TOKEN.test(token) ||
    !EXP.test(exp) ||
    !HANDOFF.test(handoff)
  ) {
    return contactUs
  }

  const check = await checkHandoff(cartId, token, exp, handoff)
  if (!check.valid) {
    const resume = check.resumeUrl && resumeRedirect(check.resumeUrl, request)
    return resume ? NextResponse.redirect(resume, 303) : contactUs
  }

  const response = NextResponse.redirect(
    new URL(`/${countryCode}/checkout?step=address`, request.url),
    303
  )
  response.cookies.set("_medusa_cart_id", cartId, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
  })
  // One use: the nonce has done its job.
  response.cookies.set("po_qn", "", { maxAge: 0, path: "/" })
  return response
}
