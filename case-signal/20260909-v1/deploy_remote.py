"""Install Case Signal alongside Fleet Evidence; never restart existing services."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile

BASE=Path('/mnt/d/KDDeployment')
spec=importlib.util.spec_from_file_location('case_signal_preservation',BASE/'case-signal-preflight-v1.py')
preservation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(preservation)
RELEASE='20260909-v1'
DEST=BASE/'CaseSignal'/'releases'/RELEASE
REPORT=BASE/'case-signal-deployment-v1.json'
UNIT=Path('/etc/systemd/system/case-signal-demo.service')
SERVICE='case-signal-demo.service'

def require(condition,message):
    if not condition: raise RuntimeError(message)

def write(path,data):
    with path.open('x',encoding='utf-8') as f: f.write(data)

def save(report):
    tmp=REPORT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report,indent=2),encoding='utf-8')
    os.replace(tmp,REPORT)

def request(path,body=None,timeout=25):
    headers={'Content-Type':'application/json'}
    if body is not None: headers['Origin']='http://127.0.0.1:8096'
    req=urllib.request.Request('http://127.0.0.1:8096'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=timeout) as response: return json.loads(response.read(10*1024*1024))

def command(argv):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=30)
    require(result.returncode==0,Path(argv[0]).name+' failed; inspect retained logs.')
    return result.stdout.strip()

def container_activity():
    ids=command(['docker','ps','-a','--quiet','--no-trunc']).split()
    require(bool(ids),'Container inventory unavailable.')
    lines=command(['docker','inspect','--format','{{.Id}} {{.State.Running}} {{.State.Status}}',*sorted(ids)]).splitlines()
    return sorted(lines)

def extract(archive,expected):
    require(archive.is_file() and 0<archive.stat().st_size<16*1024*1024,'Invalid bounded archive.')
    require(hashlib.sha256(archive.read_bytes()).hexdigest()==expected,'Archive hash mismatch.')
    require(not DEST.exists(),'Release already exists; inspect prior deployment before changing it.')
    for parent in [BASE,BASE/'CaseSignal',BASE/'CaseSignal'/'releases']:
        require(not parent.is_symlink(),'Unexpected symlink in deployment path.')
    with zipfile.ZipFile(archive) as z:
        entries=z.infolist()
        require(1<len(entries)<200 and sum(i.file_size for i in entries)<16*1024*1024,'Archive exceeds bounds.')
        seen=set()
        for item in entries:
            path=PurePosixPath(item.filename)
            require(item.filename and not path.is_absolute() and '\\' not in item.filename and ':' not in item.filename and all(p not in ('','.','..') for p in path.parts),'Unsafe archive entry.')
            require(item.filename.casefold() not in seen,'Duplicate archive entry.')
            seen.add(item.filename.casefold())
            require(stat.S_IFMT(item.external_attr>>16) in {0,stat.S_IFREG,stat.S_IFDIR},'Archive special file refused.')
            require(not item.flag_bits & 1,'Encrypted entry refused.')
        require({'server.py','kd_client.py','sources.py','static/index.html'}.issubset(seen),'Required application files missing.')
        for parent in [BASE/'CaseSignal',BASE/'CaseSignal'/'releases']:
            if not parent.exists():
                parent.mkdir(mode=0o755)
                parent.chmod(0o755)
            require(parent.is_dir() and stat.S_IMODE(parent.stat().st_mode)&0o005==0o005,'Release parent must be traversable by the service user.')
        DEST.mkdir(mode=0o755)
        for item in entries:
            path=DEST.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir(): path.mkdir(parents=True,exist_ok=True); continue
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as f: f.write(z.read(item))
            path.chmod(0o644)
        for path in [DEST,*DEST.rglob('*')]:
            if path.is_dir(): path.chmod(0o755)

def compare(before,after):
    checks={
       'fleet_service_active':after['fleet_service']=='active',
       'fleet_unit_unchanged':after['fleet_unit_sha256']==before['fleet_unit_sha256'],
       'fleet_release_unchanged':after['fleet_release']==before['fleet_release'],
       'fleet_tables_unchanged':after['fleet_tables']==before['fleet_tables'],
       'fleet_exports_unchanged':after['fleet_case_exports']==before['fleet_case_exports'],
       'container_lifecycles_unchanged':after['containers']==before['containers'],
       'other_databases_unchanged':{k:v for k,v in after['databases'].items() if k!='CASE_SIGNAL_DEMO'}=={k:v for k,v in before['databases'].items() if k!='CASE_SIGNAL_DEMO'},
       'fleet_health_ok':after['fleet_health'].get('ok') is True,
       'fleet_service_enabled':after['fleet_enabled']==before['fleet_enabled']=='enabled',
    }
    require(all(checks.values()),'Preservation check failed: '+','.join(k for k,v in checks.items() if not v))
    return checks

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--sha256',required=True)
    args=parser.parse_args()
    require(os.geteuid()==0,'Existing owner WSL root execution required.')
    require(re.fullmatch('[a-f0-9]{64}',args.sha256),'Invalid archive identity.')
    require(not REPORT.exists() and not UNIT.exists(),'A prior installation exists; inspect before any retry.')
    report={'complete':False,'release':RELEASE,'started_at':datetime.now(timezone.utc).isoformat(),'archive_sha256':args.sha256}
    try:
        before=preservation.snapshot()
        activity_before=container_activity()
        require(before['fleet_health'].get('ok') is True,'Fleet Evidence must be available before installation.')
        with socket.socket() as s: require(s.connect_ex(('127.0.0.1',8096))!=0,'Port 8096 is already occupied.')
        # Resolve the already running model; no image pulls or container changes.
        model_ids=command(['docker','ps','--filter','name=kd-llama','--quiet','--no-trunc']).split()
        require(len(model_ids)==1,'Existing generation service must be running.')
        ips=command(['docker','inspect','--format','{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}',model_ids[0]]).split()
        require(len(ips)==1,'Existing model address is ambiguous.')
        require(preservation.get('http://'+ips[0]+':8080/health').get('status')=='ok','Existing model health is not ready.')
        report['before']=before
        report['container_activity_before']=activity_before
        save(report)
        extract(args.archive,args.sha256)
        env={'KD_CONTENT_URL':before['kd_content_url'],'KD_INDEX_URL':before['kd_index_url'],
             'KD_LLM_URL':'http://'+ips[0]+':8080','KD_LLM_MODEL':'kd-local-qwen',
             'CASE_SIGNAL_SOURCE':'fixture','CASE_SIGNAL_STATE_DIR':'/var/lib/case-signal',
             'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
        text='[Unit]\nDescription=Case Signal synthetic case briefing demonstration\nAfter=network.target\n\n[Service]\nType=simple\nUser=nobody\nGroup=nogroup\n'
        text+='WorkingDirectory='+str(DEST)+'\n'
        for key,value in env.items():
            require('\n' not in value and '"' not in value,'Unsafe service configuration.')
            text+='Environment="'+key+'='+value+'"\n'
        text+='ExecStart=/usr/bin/python3 '+str(DEST/'server.py')+' --host 127.0.0.1 --port 8096\n'
        text+='Restart=always\nRestartSec=5\nStateDirectory=case-signal\nStateDirectoryMode=0700\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target\n'
        write(UNIT,text)
        command(['systemctl','daemon-reload'])
        command(['systemctl','enable','--now',SERVICE])
        report['service_started']=True
        report['unit_sha256']=hashlib.sha256(text.encode()).hexdigest()
        save(report)
        deadline=time.monotonic()+45
        while True:
            try:
                bootstrap=request('/api/bootstrap')
                break
            except Exception:
                if time.monotonic()>deadline: raise
                time.sleep(1)
        report['bootstrap_before_sync']=bootstrap
        require(bootstrap.get('app',{}).get('name')=='Case Signal' and bootstrap['app'].get('version')==RELEASE,'Wrong application or release served on port 8096.')
        require(bootstrap.get('kd',{}).get('connected') is True and bootstrap.get('source',{}).get('mode')=='fixture' and bootstrap.get('fleet',{}).get('available') is True,'Bootstrap did not establish shared KD and original demo availability.')
        require(command(['systemctl','is-active',SERVICE])=='active','New service is not active.')
        report['after']=preservation.snapshot()
        report['preservation']=compare(before,report['after'])
        report['container_activity_after']=container_activity()
        require(report['container_activity_after']==activity_before,'Existing container running states changed during installation.')
        report['preservation']['container_activity_unchanged']=True
        report['service_enabled']=command(['systemctl','is-enabled',SERVICE])=='enabled'
        report['complete']=True
        report['completed_at']=datetime.now(timezone.utc).isoformat()
        report['next']='Run the explicit synthetic source sync and its acceptance checks; installation alone does not establish KD delivery.'
        save(report)
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error)[:400]
        save(report)
        raise
    print(json.dumps({'complete':report['complete'],'report':str(REPORT)}))

if __name__=='__main__': main()
