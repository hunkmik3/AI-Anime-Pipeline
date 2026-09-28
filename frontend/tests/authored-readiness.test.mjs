import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {build} from 'esbuild';
const bundled=await build({entryPoints:[fileURLToPath(new URL('../src/automation/contracts.ts',import.meta.url))],bundle:true,platform:'node',format:'cjs',write:false,logLevel:'silent'});
const module={exports:{}};
new Function('require','module','exports',bundled.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
const {sourceReadyForShots}=module.exports;
test('authored readiness is distinct from source-frame readiness',()=>{
 const report={status:'verified',method:'authored_script',digest:'digest',signature:'server-checks-this',shot_digests:{SH01:'sha'}};
 assert.equal(sourceReadyForShots([{id:'SH01',provenance:'authored_adaptation'}],report),true);
 assert.equal(sourceReadyForShots([{id:'SH02',provenance:'authored_adaptation'}],report),false);
 assert.equal(sourceReadyForShots([{source_shots:[1]}],report),false);
 assert.equal(sourceReadyForShots([{id:'SH01',provenance:'authored_adaptation'}],{...report,signature:''}),false);
});
