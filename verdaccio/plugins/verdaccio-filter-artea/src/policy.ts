// Thin re-export barrel: policy compilation/validation lives in ./policy-compile
// and the file/HTTP/composite loaders live in ./policy-loaders. Importers keep
// using './policy' so this split stays internal.
export {
  type CompiledPolicy,
  type PolicyState,
  SEMVER_OPTS,
  parseDurationMs,
  compilePolicy,
  isNameBlocked,
  isVersionBlocked,
} from './policy-compile';
export {
  type PolicyLoader,
  type HttpLoaderOptions,
  type PolicySourceConfig,
  FilePolicyLoader,
  HttpPolicyLoader,
  CompositePolicyLoader,
  DEFAULT_POLL_INTERVAL_MS,
  DEFAULT_FAIL_GRACE_MS,
  createPolicyLoader,
} from './policy-loaders';
