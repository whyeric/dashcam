import time, cv2, mediapipe as mp

mp_pose = mp.solutions.pose

def bench(cap_w, cap_h, proc_w, n=80, show=False):
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cap_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cap_h)
    pose = mp_pose.Pose(model_complexity=0, enable_segmentation=False, smooth_landmarks=True)
    # warmup
    for _ in range(10):
        ok, f = cap.read()
        if ok:
            small = cv2.resize(f, (proc_w, int(f.shape[0]*proc_w/f.shape[1])))
            pose.process(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
    t = {"read":0.0,"prep":0.0,"proc":0.0,"draw":0.0,"show":0.0}
    cnt = 0
    aw = cv2.addWeighted
    for _ in range(n):
        a=time.perf_counter(); ok,f=cap.read(); b=time.perf_counter()
        if not ok: continue
        H,W=f.shape[:2]
        small=cv2.resize(f,(proc_w,int(H*proc_w/W))); rgb=cv2.cvtColor(small,cv2.COLOR_BGR2RGB); c=time.perf_counter()
        rgb.flags.writeable=False; pose.process(rgb); rgb.flags.writeable=True; d=time.perf_counter()
        ov=f.copy(); cv2.rectangle(ov,(0,0),(W//3,H),(0,255,0),-1); aw(ov,0.25,f,0.75,0,f)
        lo=f.copy(); cv2.line(lo,(W//3,0),(W//3,H),(255,255,255),2); aw(lo,0.7,f,0.3,0,f)
        cv2.putText(f,"CENTER",(W//3,60),cv2.FONT_HERSHEY_SIMPLEX,1.8,(0,255,0),4); e=time.perf_counter()
        if show: cv2.imshow("b",f); cv2.waitKey(1)
        g=time.perf_counter()
        t["read"]+=b-a; t["prep"]+=c-b; t["proc"]+=d-c; t["draw"]+=e-d; t["show"]+=g-e; cnt+=1
    cap.release(); pose.close()
    if show: cv2.destroyAllWindows()
    actual_w=cap.get(cv2.CAP_PROP_FRAME_WIDTH); actual_h=cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    tot=sum(t.values())/cnt*1000
    print(f"req {cap_w}x{cap_h} -> actual {actual_w:.0f}x{actual_h:.0f} | proc_w={proc_w} | frames={cnt}")
    for k in t: print(f"   {k:5s}: {t[k]/cnt*1000:6.2f} ms")
    print(f"   TOTAL: {tot:6.2f} ms  => {1000/tot:5.1f} fps (single-thread, empty scene)\n")

bench(640,480,480, show=False)
bench(640,480,256, show=False)
bench(640,480,160, show=False)
