import { redirect } from "next/navigation"

export const metadata = {
  title: "Start Your Project | PackOasis",
  description:
    "Submit your packaging requirements, production timeline, and RFQ details to PackOasis.",
}

export default async function ContactUsPage({
  searchParams,
}: {
  searchParams?: Promise<{ draft_id?: string }>
}) {
  const params = (await searchParams) || {}
  const draftQuery = params.draft_id
    ? `?draft_id=${encodeURIComponent(params.draft_id)}`
    : ""

  redirect(`/contact-us.html${draftQuery}`)
}
