import {readFile} from 'node:fs/promises';
import {parse} from 'smol-toml';
import path from 'node:path';

// Only session overrides: never copy auth tokens or edit the user's config.
export function httpSessionConfig(host, timeoutMs) {
  const provider=host.model_provider||'openai';
  if(typeof provider!=='string'||! /^[a-zA-Z0-9_-]+$/.test(provider))throw new Error('invalid-provider-config');
  const budget=Math.min(20000,Math.max(1000,timeoutMs-5000));
  const config={sandbox_mode:'read-only',approval_policy:'on-request',web_search:'disabled',
    model_reasoning_effort:'low',features:{shell_tool:false},mcp_servers:{}};
  const transport={supports_websockets:false,request_max_retries:0,stream_max_retries:0,
    stream_idle_timeout_ms:budget};
  if(provider==='openai') {
    // Built-in provider tables cannot be redefined. Use a session-only alias
    // with native OpenAI auth; leave the host's model and credentials untouched.
    const alias='xiaoman_acp_http';
    config.model_provider=alias;
    config.model_providers={[alias]:{name:'OpenAI HTTP (Xiaoman session)',wire_api:'responses',
      requires_openai_auth:true,...transport}};
    if(typeof host.openai_base_url==='string')config.model_providers[alias].base_url=host.openai_base_url;
  } else {
    // Dotted overrides merge into an existing provider instead of replacing
    // its base URL/auth/headers. Those values must never enter vendor logs.
    for(const [key,value] of Object.entries(transport))config[`model_providers.${provider}.${key}`]=value;
  }
  return config;
}

export async function readHttpSessionConfig(codexHome,timeoutMs) {
  let text;
  try {text=await readFile(path.join(codexHome,'config.toml'),'utf8');}
  catch(error){if(error.code==='ENOENT')return httpSessionConfig({},timeoutMs);throw new Error('provider-config-unreadable');}
  try {return httpSessionConfig(parse(text),timeoutMs);}
  catch {throw new Error('invalid-provider-config');}
}
