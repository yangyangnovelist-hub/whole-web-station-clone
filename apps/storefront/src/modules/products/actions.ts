"use server"

import { redirect } from "next/navigation"

import { addProjectDraftItem } from "@lib/data/rfq"

function getField(formData: FormData, key: string) {
  return formData.get(key)?.toString().trim() || ""
}

export async function startProjectFromProduct(formData: FormData) {
  const countryCode = getField(formData, "country_code") || "us"
  const productTitle = getField(formData, "product_title")
  const productHandle = getField(formData, "product_handle")
  const productImage = getField(formData, "product_image")
  const productSku = getField(formData, "product_sku")
  const variantTitle = getField(formData, "variant_title")
  const selectedOptions = getField(formData, "selected_options")

  if (!productTitle || !productHandle) {
    redirect(`/contact-us.html`)
  }

  const notes = [variantTitle, selectedOptions].filter(Boolean).join(" | ")

  const draft = await addProjectDraftItem({
    title: productTitle,
    quantity: 1,
    product_handle: productHandle,
    sku: productSku || undefined,
    url: `/${countryCode}/products/${productHandle}`,
    image: productImage || undefined,
    notes: notes || undefined,
    metadata: {
      source: "product_detail",
      variant_title: variantTitle || undefined,
      selected_options: selectedOptions || undefined,
    },
  })

  const draftQuery = draft?.id ? `?draft_id=${encodeURIComponent(draft.id)}` : ""

  redirect(`/contact-us.html${draftQuery}`)
}
