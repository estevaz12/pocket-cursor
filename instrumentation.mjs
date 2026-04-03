/**
 * Optional OpenTelemetry tracing hook for Node-side tooling (md_to_image / Puppeteer).
 * The bridge itself is Python; this file exists so shared observability patterns are visible.
 */
import { trace } from '@opentelemetry/api';

export const tracer = trace.getTracer('pocket-cursor-node');
