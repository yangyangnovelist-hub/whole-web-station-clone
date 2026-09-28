import { randomBytes, timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

const RFQ_ID = /^[A-Za-z0-9_-]{1,64}$/
const TOKEN = /^[A-Za-z0-9_-]{1,128}$/
const CART_ID = /^cart_[0-9A-Z]{26}$/
/** The po_rs form nonce: 32 random bytes, base64url. */
const NONCE = /^[A-Za-z0-9_-]{43}$/
const NONCE_COOKIE = "po_rs"

const BACKEND_URL = (
  process.env.MEDUSA_BACKEND_URL || "http://localhost:9000"
).replace(/\/$/, "")

/**
 * Asks the backend for a cart for this signed quote link: the cart this
 * browser already holds when that is still the quote's current, unexpired
 * cart, otherwise a fresh one. The cart id comes back only on this
 * server-to-server call and goes straight into this browser's HttpOnly
 * cookie. Any failure counts as no cart.
 */
async function resumeCart(rfq: string, token: string, heldCartId?: string) {
  try {
    const response = await fetch(`${BACKEND_URL}/packoasis/resume-cart`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        rfq,
        token,
        ...(heldCartId ? { current_cart_id: heldCartId } : {}),
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    })
    if (!response.ok) {
      return null
    }
    const body = (await response.json()) as {
      cart_id?: unknown
      country_code?: unknown
    }
    if (typeof body?.cart_id !== "string" || !CART_ID.test(body.cart_id)) {
      return null
    }
    const country =
      typeof body.country_code === "string"
        ? body.country_code.toLowerCase()
        : ""
    return {
      cartId: body.cart_id,
      countryCode: /^[a-z]{2}$/.test(country)
        ? country
        : (process.env.NEXT_PUBLIC_DEFAULT_REGION ?? "us").toLowerCase(),
    }
  } catch {
    return null
  }
}

function contactUs(request: NextRequest) {
  return NextResponse.redirect(new URL("/contact-us.html", request.url), 303)
}

/** Back to the GET confirmation page, which asks the buyer to confirm. */
function confirmRedirect(request: NextRequest, rfq: string, token: string) {
  const confirm = new URL("/api/quote-resume", request.url)
  confirm.searchParams.set("rfq", rfq)
  confirm.searchParams.set("token", token)
  return NextResponse.redirect(confirm, 303)
}

/** Constant-time check that the form's nonce is this browser's po_rs cookie. */
function sameNonce(field: string, cookie: string) {
  if (!NONCE.test(field) || !NONCE.test(cookie)) {
    return false
  }
  const encoder = new TextEncoder()
  return timingSafeEqual(encoder.encode(field), encoder.encode(cookie))
}

function nonceCookie(value: string, maxAge: number) {
  return {
    name: NONCE_COOKIE,
    value,
    maxAge,
    httpOnly: true,
    sameSite: "strict" as const,
    secure: process.env.NODE_ENV === "production",
    path: "/api/quote-resume",
  }
}

/**
 * rfq, token and nonce only ever hold [A-Za-z0-9_-] (checked by the callers
 * or generated here).
 */
function confirmPage(rfq: string, token: string, nonce: string) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Complete your PackOasis order</title>
<style>
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#f6f6f4;color:#1f2328}
main{max-width:30rem;margin:12vh auto;padding:2rem;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:1.4rem;margin:0 0 .75rem}
p{line-height:1.5;margin:0 0 1rem;color:#4a4f55}
button{font:inherit;font-weight:600;padding:.8rem 1.4rem;border:0;border-radius:8px;background:#1f2328;color:#fff;cursor:pointer}
a{color:inherit}
</style>
</head>
<body>
<main>
<h1>Complete your PackOasis order</h1>
<p>Continue to checkout to order your quote. If it has expired, it is re-priced at today's prices first, in a new checkout.</p>
<p>Already checking out this quote in another tab of this browser? Continuing here takes you back to that checkout. On another device or in another browser? Finish there: continuing here starts a new checkout, and the other one can no longer be completed.</p>
<form method="post" action="/api/quote-resume">
<input type="hidden" name="rfq" value="${rfq}">
<input type="hidden" name="token" value="${token}">
<input type="hidden" name="rs" value="${nonce}">
<button type="submit">Continue to checkout</button>
</form>
<p style="margin-top:1.5rem">Questions? <a href="/contact-us.html">Contact us</a>.</p>
</main>
</body>
</html>`
}

/**
 * "Complete my order" link from quote emails. Opening it only shows a
 * confirmation page: mail scanners and link previews fetch links, and a
 * resume can mint a fresh cart that supersedes the quote's current one (such
 * as a cart the buyer is checking out with in another browser). The cart is
 * only made when the buyer presses the button (POST below). The page's form
 * carries a random nonce that is also set as this browser's po_rs cookie
 * (SameSite=Strict, only sent to this route), and the POST requires both to
 * match. Another site can post the form's fields but cannot read the cookie;
 * only something able to set cookies for this site (such as a compromised
 * subdomain) could forge a match.
 */
export async function GET(request: NextRequest) {
  const rfq = request.nextUrl.searchParams.get("rfq") ?? ""
  const token = request.nextUrl.searchParams.get("token") ?? ""
  if (!RFQ_ID.test(rfq) || !TOKEN.test(token)) {
    return contactUs(request)
  }
  const nonce = randomBytes(32).toString("base64url")
  const response = new NextResponse(confirmPage(rfq, token, nonce), {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "referrer-policy": "same-origin",
      "x-robots-tag": "noindex",
      "content-security-policy":
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    },
  })
  response.cookies.set(nonceCookie(nonce, 30 * 60))
  return response
}

/**
 * The confirmation page's form: the backend returns the quote's cart this
 * browser already holds if it is still current, or mints a fresh cart for the
 * quote (re-priced if it expired), which is attached to this browser before
 * jumping to the address step. A forwarded link never gets its opener a cart
 * somebody else can read: an existing cart only comes back to the browser
 * whose cookie already holds it. Posts from other sites (a
 * Sec-Fetch-Site other than same-origin) and posts without this browser's
 * po_rs nonce are sent to the confirmation page instead.
 */
export async function POST(request: NextRequest) {
  let form: FormData | null = null
  try {
    form = await request.formData()
  } catch {
    form = null
  }
  const rfq = String(form?.get("rfq") ?? "")
  const token = String(form?.get("token") ?? "")
  if (!RFQ_ID.test(rfq) || !TOKEN.test(token)) {
    return contactUs(request)
  }

  const site = request.headers.get("sec-fetch-site")
  const nonce = request.cookies.get(NONCE_COOKIE)?.value ?? ""
  if (
    (site && site !== "same-origin") ||
    !sameNonce(String(form?.get("rs") ?? ""), nonce)
  ) {
    return confirmRedirect(request, rfq, token)
  }

  const heldCartId = request.cookies.get("_medusa_cart_id")?.value
  const resumed = await resumeCart(
    rfq,
    token,
    heldCartId && CART_ID.test(heldCartId) ? heldCartId : undefined
  )
  if (!resumed) {
    const failed = contactUs(request)
    failed.cookies.set(nonceCookie("", 0))
    return failed
  }

  const response = NextResponse.redirect(
    new URL(`/${resumed.countryCode}/checkout?step=address`, request.url),
    303
  )
  response.cookies.set(nonceCookie("", 0))
  // Lax (not Strict) so the cart is still there when the buyer comes back
  // through a link on another site, such as a later quote email.
  response.cookies.set("_medusa_cart_id", resumed.cartId, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
  })
  return response
}
