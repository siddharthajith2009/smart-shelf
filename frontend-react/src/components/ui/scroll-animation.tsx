"use client";

import * as React from "react";
import { motion, type MotionProps } from "framer-motion";

import { cn } from "@/lib/utils";

type ScrollAnimationProps = {
  children: React.ReactNode;
  className?: string;
  /** Seconds to wait before the reveal starts once in view. */
  delay?: number;
  /** Reveal duration in seconds. */
  duration?: number;
  /** Passed straight to framer-motion's viewport option. */
  viewport?: MotionProps["viewport"];
};

export function ScrollAnimation({
  children,
  className,
  delay = 0,
  duration = 0.6,
  viewport = { once: true, amount: 0.4 },
}: ScrollAnimationProps) {
  return (
    <motion.div
      className={cn(className)}
      initial={{ opacity: 0, y: 48, scale: 0.98 }}
      whileInView={{ opacity: 1, y: 0, scale: 1 }}
      viewport={viewport}
      transition={{ duration, delay, ease: [0.25, 0.1, 0.25, 1] }}
    >
      {children}
    </motion.div>
  );
}
