import type { Metadata } from "next";
import { Gallery } from "@/components/gallery/gallery";

export const metadata: Metadata = { title: "Attack gallery" };

export default function GalleryPage() {
  return <Gallery />;
}
