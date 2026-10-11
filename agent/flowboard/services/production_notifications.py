"""Persistent production outcomes derived from jobs, never from an open browser."""
from datetime import timezone
from sqlmodel import select
from flowboard.db.models import AutomationProject,AutomationJob,VideoAnalysis
from flowboard.services.automation_jobs import ACTIVE


def inbox(session,owner=None):
    projects=session.exec(select(AutomationProject)).all()
    projects={p.id:p for p in projects if owner is None or p.owner_user_id in (None,owner)}
    if not projects: return {'events':[],'active_projects':0}
    records=session.exec(select(AutomationJob).where(AutomationJob.project_id.in_(list(projects)),
        AutomationJob.kind.in_(['source','production_run'])).order_by(AutomationJob.created_at)).all()
    latest={j.project_id:j for j in records}
    active={j.project_id for j in records if j.status in ACTIVE}
    events=[]
    for pid,j in latest.items():
        review=j.status=='paused' and j.result.get('stage')=='master_review'
        if pid in active or not review and j.status not in {'succeeded','failed','blocked','unknown','waiting_user'}: continue
        project=projects[pid];name=project.title or project.name
        stamp=j.updated_at.replace(tzinfo=timezone.utc) if j.updated_at.tzinfo is None else j.updated_at
        successful=j.status=='succeeded'
        detail='Phim đã ghép xong, bạn có thể mở để xem.' if successful and j.kind=='production_run' else 'Phân tích nguồn đã xong.' if successful else 'Tool đã lưu kết quả. Mở dự án để xem các mục còn cần xử lý.'
        if j.kind=='production_run' and j.payload.get('config',{}).get('mode')!='render' and successful:
            detail='Material và prompt đã chuẩn bị xong.'
        if review or j.status=='waiting_user':detail='Đã tạo xong sheet chính. Chốt tạo hình để tiếp tục.'
        source=session.exec(select(VideoAnalysis).where(VideoAnalysis.automation_project_id==pid).order_by(VideoAnalysis.created_at.desc())).first()
        counters=((source.progress or {}).get('steps',{}).get('profiles',{}) if source else {})
        if not successful and counters.get('pending'):
            detail=f"Đã lập hồ sơ {counters.get('done',0)}/{counters.get('total',0)} shot; {counters['pending']} shot và phần phụ thuộc cần đối soát."
        events.append({'id':str(j.id)+':'+j.status+':'+stamp.isoformat(),'project_id':str(pid),'project_name':name,
            'status':j.status,'title':'Đã hoàn tất' if successful else 'Cần bạn quyết định',
            'message':detail,'updated_at':stamp.isoformat()})
    return {'events':sorted(events,key=lambda e:e['updated_at'],reverse=True)[:30],'active_projects':len(active)}
