import {
  finalizeGoogleCustomerAuth,
  transferCart,
} from "@lib/data/customer"
import { NextRequest, NextResponse } from "next/server"

const buildRedirectUrl = (request: NextRequest, path: string) =>
  new URL(path, request.url)

const setAuthCookie = (response: NextResponse, token: string) => {
  response.cookies.set("_medusa_jwt", token, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
  })
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ countryCode: string }> }
) {
  const { countryCode } = await context.params

  try {
    const searchParams = new URL(request.url).searchParams
    const { token } = await finalizeGoogleCustomerAuth(
      Object.fromEntries(searchParams.entries())
    )

    await transferCart(token)

    const response = NextResponse.redirect(
      buildRedirectUrl(request, `/${countryCode}/account`)
    )
    setAuthCookie(response, token)

    return response
  } catch {
    return NextResponse.redirect(
      buildRedirectUrl(
        request,
        `/${countryCode}/account?auth_error=google_callback_failed`
      )
    )
  }
}
