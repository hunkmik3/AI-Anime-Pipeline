from flowboard.services.flowstudio import r2


def test_same_basename_keeps_distinct_immutable_content_and_reuses_identical_upload(tmp_path,monkeypatch):
    objects={};puts=[]
    class Client:
        def put_object(self,**kwargs):
            objects[kwargs['Key']]=kwargs['Body'];puts.append(kwargs['Key'])
    monkeypatch.setattr(r2,'_uploaded',set())
    monkeypatch.setattr(r2,'_get_client',lambda:Client())
    monkeypatch.setattr(r2,'public_base',lambda:'https://example.test')
    one=tmp_path/'a'/'original-context.mp4';two=tmp_path/'b'/'original-context.mp4'
    one.parent.mkdir();two.parent.mkdir()
    one.write_bytes(b'first source interval');two.write_bytes(b'second source interval')
    url1=r2.upload_file(one,content_addressed=True)
    url2=r2.upload_file(two,content_addressed=True)
    assert url1!=url2 and len(puts)==2
    assert objects[url1.removeprefix('https://example.test/')]==one.read_bytes()
    assert objects[url2.removeprefix('https://example.test/')]==two.read_bytes()
    assert r2.upload_file(one,content_addressed=True)==url1 and len(puts)==2
    one.write_bytes(b'corrected first interval')
    url3=r2.upload_file(one,content_addressed=True)
    assert url3 not in {url1,url2} and len(puts)==3
    assert objects[url1.removeprefix('https://example.test/')]==b'first source interval'
