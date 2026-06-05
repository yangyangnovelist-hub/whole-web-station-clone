"use server"

import { removeProjectDraftItem } from "@lib/data/rfq"

export async function removeProjectDrawerItem(itemId: string) {
  if (!itemId) {
    return null
  }

  return removeProjectDraftItem(itemId)
}

