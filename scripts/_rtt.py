import socket, time, sys
host = sys.argv[1]
port = int(sys.argv[2])
for _ in range(5):
    s = socket.socket()
    t = time.time()
    s.connect((host, port))
    print(f"{(time.time()-t)*1000:.1f} ms")
    s.close()
