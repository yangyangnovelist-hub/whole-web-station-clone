"use client"

import LocalizedClientLink from "@modules/common/components/localized-client-link"
import { useSearchParams } from "next/navigation"
import { useState } from "react"

import Register from "@modules/account/components/register"
import Login from "@modules/account/components/login"

export enum LOGIN_VIEW {
  SIGN_IN = "sign-in",
  REGISTER = "register",
}

const LoginTemplate = () => {
  const searchParams = useSearchParams()
  const [currentView, setCurrentView] = useState(LOGIN_VIEW.SIGN_IN)

  const errorCode = searchParams.get("auth_error")
  const authError =
    errorCode === "google_unavailable"
      ? "Google sign-in is not available yet. Verify the Medusa Google provider is configured in production."
      : errorCode === "google_callback_failed"
        ? "Google authorization returned, but the account could not be finalized. Please try again or use email sign-in."
        : null

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(360px,470px)]">
      <section className="rounded-[34px] border border-[#dce8da] bg-[radial-gradient(circle_at_top_left,_rgba(145,216,170,0.18),_transparent_42%),linear-gradient(180deg,_rgba(255,255,255,0.96)_0%,_rgba(247,251,244,0.98)_100%)] p-8 shadow-[0_30px_80px_rgba(16,42,54,0.08)] small:p-10">
        <div className="max-w-xl space-y-8">
          <div className="space-y-4">
            <span className="inline-flex rounded-full border border-[#cde6cf] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.35em] text-[#3ea26f]">
              PackOasis Account Hub
            </span>
            <h2 className="font-serif text-[3rem] leading-[0.95] text-[#11263a] small:text-[4.2rem]">
              Keep quotes, drafts, and production details in one place.
            </h2>
            <p className="max-w-2xl text-lg leading-8 text-[#5e6f7f]">
              This account layer now sits on the PackOasis storefront instead of
              the Medusa starter shell. The workflow stays tied to your mirrored
              site experience while the backend handles authentication and quote
              continuity.
            </p>
          </div>

          <div className="grid gap-4 small:grid-cols-2">
            <div className="rounded-[28px] border border-[#d7e5d6] bg-white/80 p-5">
              <p className="text-[11px] font-semibold uppercase tracking-[0.3em] text-[#3ea26f]">
                Workflow
              </p>
              <p className="mt-3 text-base leading-7 text-[#17303b]">
                Save RFQs, revisit product selections, and move from inquiry to
                order history without switching systems.
              </p>
            </div>
            <div className="rounded-[28px] border border-[#d7e5d6] bg-white/80 p-5">
              <p className="text-[11px] font-semibold uppercase tracking-[0.3em] text-[#3ea26f]">
                Connected Pages
              </p>
              <p className="mt-3 text-base leading-7 text-[#17303b]">
                Account actions stay aligned with the mirrored catalog, quote
                entry, and product exploration paths.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap gap-3">
            <LocalizedClientLink
              href="/contact-us.html"
              className="inline-flex items-center rounded-full bg-[#13273b] px-6 py-3 text-sm font-semibold text-white transition hover:bg-[#1b3550]"
            >
              Start a quote
            </LocalizedClientLink>
            <LocalizedClientLink
              href="/store"
              className="inline-flex items-center rounded-full border border-[#ceddcb] px-6 py-3 text-sm font-semibold text-[#17303b] transition hover:border-[#8cc9a8] hover:bg-white"
            >
              Browse catalog
            </LocalizedClientLink>
          </div>
        </div>
      </section>

      <section className="rounded-[34px] border border-[#dce8da] bg-white/95 p-8 shadow-[0_30px_80px_rgba(16,42,54,0.1)] small:p-10">
        {authError && (
          <div className="mb-6 rounded-[24px] border border-[#f0d4bf] bg-[#fff7f0] px-5 py-4 text-sm leading-6 text-[#8b5b3e]">
            {authError}
          </div>
        )}

        {currentView === LOGIN_VIEW.SIGN_IN ? (
          <Login setCurrentView={setCurrentView} />
        ) : (
          <Register setCurrentView={setCurrentView} />
        )}
      </section>
    </div>
  )
}

export default LoginTemplate
