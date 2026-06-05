"use client"

import { clx } from "@medusajs/ui"
import { ArrowRightOnRectangle } from "@medusajs/icons"
import { useParams, usePathname } from "next/navigation"

import ChevronDown from "@modules/common/icons/chevron-down"
import User from "@modules/common/icons/user"
import MapPin from "@modules/common/icons/map-pin"
import Package from "@modules/common/icons/package"
import LocalizedClientLink from "@modules/common/components/localized-client-link"
import { HttpTypes } from "@medusajs/types"
import { signout } from "@lib/data/customer"

const AccountNav = ({
  customer,
}: {
  customer: HttpTypes.StoreCustomer | null
}) => {
  const route = usePathname()
  const { countryCode } = useParams() as { countryCode: string }

  const handleLogout = async () => {
    await signout(countryCode)
  }

  return (
    <div className="rounded-[34px] border border-[#dce8da] bg-white/92 p-6 shadow-[0_30px_80px_rgba(16,42,54,0.08)] small:p-8">
      <div className="small:hidden" data-testid="mobile-account-nav">
        {route !== `/${countryCode}/account` ? (
          <LocalizedClientLink
            href="/account"
            className="flex items-center gap-x-2 py-2 text-sm font-semibold text-[#17303b]"
            data-testid="account-main-link"
          >
            <>
              <ChevronDown className="transform rotate-90" />
              <span>Account</span>
            </>
          </LocalizedClientLink>
        ) : (
          <>
            <div className="mb-5">
              <span className="inline-flex rounded-full border border-[#cde6cf] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.3em] text-[#3ea26f]">
                PackOasis account
              </span>
              <div className="mt-4 font-serif text-3xl text-[#11263a]">
                Hello {customer?.first_name || "there"}
              </div>
            </div>
            <div className="text-base-regular">
              <ul>
                <li>
                  <LocalizedClientLink
                    href="/account/profile"
                    className="flex items-center justify-between rounded-[20px] border border-[#e4ece2] px-5 py-4 text-[#17303b]"
                    data-testid="profile-link"
                  >
                    <>
                      <div className="flex items-center gap-x-2">
                        <User size={20} />
                        <span>Profile</span>
                      </div>
                      <ChevronDown className="transform -rotate-90" />
                    </>
                  </LocalizedClientLink>
                </li>
                <li>
                  <LocalizedClientLink
                    href="/account/addresses"
                    className="mt-3 flex items-center justify-between rounded-[20px] border border-[#e4ece2] px-5 py-4 text-[#17303b]"
                    data-testid="addresses-link"
                  >
                    <>
                      <div className="flex items-center gap-x-2">
                        <MapPin size={20} />
                        <span>Addresses</span>
                      </div>
                      <ChevronDown className="transform -rotate-90" />
                    </>
                  </LocalizedClientLink>
                </li>
                <li>
                  <LocalizedClientLink
                    href="/account/orders"
                    className="mt-3 flex items-center justify-between rounded-[20px] border border-[#e4ece2] px-5 py-4 text-[#17303b]"
                    data-testid="orders-link"
                  >
                    <div className="flex items-center gap-x-2">
                      <Package size={20} />
                      <span>Orders</span>
                    </div>
                    <ChevronDown className="transform -rotate-90" />
                  </LocalizedClientLink>
                </li>
                <li>
                  <button
                    type="button"
                    className="mt-3 flex w-full items-center justify-between rounded-[20px] border border-[#e4ece2] px-5 py-4 text-[#17303b]"
                    onClick={handleLogout}
                    data-testid="logout-button"
                  >
                    <div className="flex items-center gap-x-2">
                      <ArrowRightOnRectangle />
                      <span>Log out</span>
                    </div>
                    <ChevronDown className="transform -rotate-90" />
                  </button>
                </li>
              </ul>
            </div>
          </>
        )}
      </div>
      <div className="hidden small:block" data-testid="account-nav">
        <div className="space-y-7">
          <div>
            <span className="inline-flex rounded-full border border-[#cde6cf] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.3em] text-[#3ea26f]">
              PackOasis account
            </span>
            <h3 className="mt-5 font-serif text-[2rem] leading-none text-[#11263a]">
              Hello {customer?.first_name || "there"}.
            </h3>
            <p className="mt-3 text-sm leading-6 text-[#697986]">
              Keep packaging inquiries, addresses, and order history tied to
              your storefront workspace.
            </p>
          </div>
          <div className="text-base-regular">
            <ul className="mb-0 flex flex-col gap-y-3">
              <li>
                <AccountNavLink
                  href="/account"
                  route={route!}
                  data-testid="overview-link"
                >
                  Overview
                </AccountNavLink>
              </li>
              <li>
                <AccountNavLink
                  href="/account/profile"
                  route={route!}
                  data-testid="profile-link"
                >
                  Profile
                </AccountNavLink>
              </li>
              <li>
                <AccountNavLink
                  href="/account/addresses"
                  route={route!}
                  data-testid="addresses-link"
                >
                  Addresses
                </AccountNavLink>
              </li>
              <li>
                <AccountNavLink
                  href="/account/orders"
                  route={route!}
                  data-testid="orders-link"
                >
                  Orders
                </AccountNavLink>
              </li>
              <li>
                <button
                  type="button"
                  onClick={handleLogout}
                  className="flex w-full items-center gap-3 rounded-[20px] border border-[#e4ece2] px-5 py-4 text-left text-sm font-semibold text-[#17303b] transition hover:border-[#8cc9a8] hover:bg-[#f8fcf6]"
                  data-testid="logout-button"
                >
                  <ArrowRightOnRectangle />
                  Log out
                </button>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}

type AccountNavLinkProps = {
  href: string
  route: string
  children: React.ReactNode
  "data-testid"?: string
}

const AccountNavLink = ({
  href,
  route,
  children,
  "data-testid": dataTestId,
}: AccountNavLinkProps) => {
  const { countryCode }: { countryCode: string } = useParams()

  const active = route.split(countryCode)[1] === href

  return (
    <LocalizedClientLink
      href={href}
      className={clx(
        "flex items-center rounded-[20px] border px-5 py-4 text-sm font-semibold transition",
        {
          "border-[#9ed3af] bg-[#f3fbf2] text-[#17303b] shadow-[0_18px_42px_rgba(62,162,111,0.08)]":
            active,
          "border-[#e4ece2] text-[#5e6f7f] hover:border-[#8cc9a8] hover:bg-[#f8fcf6] hover:text-[#17303b]":
            !active,
        }
      )}
      data-testid={dataTestId}
    >
      {children}
    </LocalizedClientLink>
  )
}

export default AccountNav
