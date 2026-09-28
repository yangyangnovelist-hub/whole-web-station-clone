import { NextRequest, NextResponse } from "next/server"

const CART_ID = /^cart_[0-9A-Z]{26}$/
const TOKEN = /^[A-Za-z0-9_-]{1,128}$/

const BACKEND_URL = (
  process.env.MEDUSA_BACKEND_URL || "http://localhost:9000"
).replace(/\/$/, "")

/**
 * Asks the backend whether `token` is its signature for this cart hand-off,
 * so a link built with somebody else's cart id cannot plant that cart in the
 * buyer's browser. Any failure counts as invalid.
 */
async function isSignedCheckout(cartId: string, token: string) {
  try {
    const url = new URL(`${BACKEND_URL}/packoasis/checkout-token`)
    url.searchParams.set("cart_id", cartId)
    url.searchParams.set("token", token)
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    })
    if (!response.ok) {
      return false
    }
    const body = (await response.json()) as { valid?: unknown }
    return body?.valid === true
  } catch {
    return false
  }
}

/**
 * Hand-off from the instant quote widget / quote emails to checkout: the
 * backend already created a cart with the locked quote price, so attach it to
 * this browser and jump straight to the address step.
 */
export async function GET(request: NextRequest) {
  const cartId = request.nextUrl.searchParams.get("cart_id") ?? ""
  const token = request.nextUrl.searchParams.get("token") ?? ""
  const country = (
    request.nextUrl.searchParams.get("country") ??
    process.env.NEXT_PUBLIC_DEFAULT_REGION ??
    "us"
  ).toLowerCase()
  const countryCode = /^[a-z]{2}$/.test(country) ? country : "us"

  if (
    !CART_ID.test(cartId) ||
    !TOKEN.test(token) ||
    !(await isSignedCheckout(cartId, token))
  ) {
    return NextResponse.redirect(new URL("/contact-us.html", request.url), 303)
  }

  const response = NextResponse.redirect(
    new URL(`/${countryCode}/checkout?step=address`, request.url),
    303
  )
  // Lax (not Strict) so the cookie survives the redirect when the buyer
  // arrives from an email link on another site.
  response.cookies.set("_medusa_cart_id", cartId, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
  })
  return response
}
