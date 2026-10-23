import { motion, useReducedMotion } from "framer-motion";

/** The glowing Cooper orb. `busy` makes it pulse faster while an answer is being worked on. */
export function Orb({ size = 120, busy = false }: { size?: number; busy?: boolean }) {
  const still = useReducedMotion();
  return (
    <div className="orb-wrap" style={{ width: size * 1.8, height: size * 1.8 }} aria-hidden>
      <motion.div
        className="orb-glow"
        style={{ width: size * 1.7, height: size * 1.7 }}
        animate={still ? undefined : { scale: [1, busy ? 1.18 : 1.08, 1], opacity: [0.55, 0.85, 0.55] }}
        transition={{ duration: busy ? 1.4 : 4, repeat: Infinity, ease: "easeInOut" }}
      />
      <motion.div
        className="orb"
        style={{ width: size, height: size }}
        animate={still ? undefined : { rotate: 360 }}
        transition={{ duration: busy ? 6 : 18, repeat: Infinity, ease: "linear" }}
      >
        <div className="orb-core" />
        <div className="orb-shine" />
      </motion.div>
    </div>
  );
}
