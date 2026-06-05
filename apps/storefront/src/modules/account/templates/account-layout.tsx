import React from "react"

import UnderlineLink from "@modules/common/components/interactive-link"

import AccountNav from "../components/account-nav"
import { HttpTypes } from "@medusajs/types"

interface AccountLayoutProps {
  customer: HttpTypes.StoreCustomer | null
  children: React.ReactNode
}

const AccountLayout: React.FC<AccountLayoutProps> = ({
  customer,
  children,
}) => {
  return (
    <div
      className="relative flex-1 overflow-hidden bg-[#f2f6ef] py-8 small:py-14"
      data-testid="account-page"
    >
      <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(rgba(134,160,126,0.11)_1px,transparent_1px),linear-gradient(90deg,rgba(134,160,126,0.11)_1px,transparent_1px)] bg-[size:72px_72px] opacity-70" />
      <div className="pointer-events-none absolute inset-x-0 top-0 h-64 bg-[radial-gradient(circle_at_top_left,rgba(121,207,156,0.22),transparent_42%)]" />

      <div className="content-container relative z-10 mx-auto flex max-w-7xl flex-col gap-6">
        {customer ? (
          <div className="grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
            <div>
              <AccountNav customer={customer} />
            </div>
            <div className="rounded-[34px] border border-[#dce8da] bg-white/95 p-6 shadow-[0_30px_80px_rgba(16,42,54,0.1)] small:p-8">
              {children}
            </div>
          </div>
        ) : (
          children
        )}

        <div className="grid gap-6 rounded-[34px] border border-[#dce8da] bg-white/90 p-6 shadow-[0_25px_70px_rgba(16,42,54,0.08)] small:p-8 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
          <div className="max-w-2xl">
            <p className="text-[11px] font-semibold uppercase tracking-[0.32em] text-[#3ea26f]">
              Need help
            </p>
            <h3 className="mt-3 font-serif text-3xl leading-tight text-[#11263a]">
              Need a hand with account access or quote coordination?
            </h3>
            <p className="mt-3 text-base leading-7 text-[#5e6f7f]">
              Keep the mirrored storefront experience, but route account support
              through PackOasis operations and customer service.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <UnderlineLink href="/contact-us.html">Request support</UnderlineLink>
            <UnderlineLink href="/faq.html">FAQ</UnderlineLink>
          </div>
        </div>
      </div>
    </div>
  )
}

export default AccountLayout
