import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Int32MultiArray
from cv_bridge import CvBridge
import numpy as np
import cv2
from ultralytics import YOLO
import torch
import torch.nn as nn



class LSTMModel(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, num_classes):
        super(LSTMModel, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.num_classes = num_classes
        self.lstm1 = nn.LSTM(input_size, hidden_size, num_layers=1, batch_first=True)
        self.lstm2 = nn.LSTM(hidden_size, hidden_size * 2, num_layers=1, batch_first=True)
        self.lstm3 = nn.LSTM(hidden_size * 2, hidden_size * 4, num_layers=1, batch_first=True)
        self.dropout1 = nn.Dropout(0.1)
        self.lstm4 = nn.LSTM(hidden_size * 4, hidden_size * 2, num_layers=1, batch_first=True)
        self.lstm5 = nn.LSTM(hidden_size * 2, hidden_size, num_layers=1, batch_first=True)
        self.lstm6 = nn.LSTM(hidden_size, hidden_size // 2, num_layers=1, batch_first=True)
        self.dropout2 = nn.Dropout(0.1)
        self.lstm7 = nn.LSTM(hidden_size // 2, hidden_size // 4, num_layers=1, batch_first=True)
        self.fc = nn.Linear(hidden_size // 4, num_classes)

    def forward(self, x):
        h0 = torch.zeros(1, x.size(0), self.hidden_size).to(x.device)
        c0 = torch.zeros(1, x.size(0), self.hidden_size).to(x.device)

        out, _ = self.lstm1(x, (h0, c0))
        out, _ = self.lstm2(out)
        out, _ = self.lstm3(out)
        out = self.dropout1(out)
        out, _ = self.lstm4(out)
        out, _ = self.lstm5(out)
        out, _ = self.lstm6(out)
        out = self.dropout2(out)
        out, _ = self.lstm7(out)
        out = self.fc(out[:, -1, :])
        return out



## 하이퍼파라미터 설정
input_size = 34  # 데이터의 차원 (예: x와 y 좌표)
hidden_size = 64  # LSTM의 은닉 상태 크기
num_layers = 2  # LSTM 레이어 수
num_classes = 2  # 분류할 클래스 수 (0 또는 1)


# LSTMModel 생성
loaded_model = LSTMModel(input_size, hidden_size, num_layers, num_classes)

# 불러온 모델의 가중치 로드
loaded_model.load_state_dict(torch.load('lstm_long_data_model_v1.pth'))
loaded_model.eval()

frame_keypoints_xyn = [[]] # id별로 keypoints 저장하는 공간 (id, stack, keypoints)

processtime = []

class DetectLSTMSegNode(Node):
    def __init__(self):
        super().__init__('lstm_seg_node')
        self.colcor_subscriber = self.create_subscription(
            Image,
            '/camera/color/image_raw', #이미지 토픽
            self.color_image_callback,
            10
        )
        self.pose_publisher = self.create_publisher(
            Int32MultiArray,
            '/lstm_pose_seg',
            10
        )
        self.seg_publisher = self.create_publisher(
            Int32MultiArray,
            '/person_seg_tracking',
            10
        )
        self.cv_bridge = CvBridge()
        self.model = YOLO('yolov8n-pose.pt')

    def calcualte_processtime(self, now_time):
        if (len(processtime) <= 1050): # todo: 실행시간 계산용
            processtime.append(self.get_clock().now().nanoseconds - now_time)
        if (len(processtime) == 1050):
            data_array = np.array(processtime[50:])
            mean = np.mean(data_array)
            median = np.median(data_array)
            std_dev = np.std(data_array)
            min_value = np.min(data_array)
            max_value = np.max(data_array)
            print(f'Mean: {mean}')
            print(f'Median: {median}')
            print(f'Standard Deviation: {std_dev}')
            print(f'Min: {min_value}')
            print(f'Max: {max_value}')

    def color_image_callback(self, msg):
        color_image = self.cv_bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')

        results = self.model.track(color_image,
                    persist=True, # persisting tracks between frames
                    verbose=False, # no print
                    imgsz=(480,640), device="cuda:0")
        
        # yolo 적용 여부 확인용
        # cv2.imshow("YOLOv8 Tracking", results[0].plot()) # show result
        # cv2.waitKey(1)

        id_keypionts = [] # for publish bounding box
        for person in results[0]:
            try:
                # now_time = self.get_clock().now().nanoseconds # todo: 실행시간 계산용
                keypoints = person.keypoints
                xyn = keypoints.xyn.cpu().numpy()
                xyn_list = xyn.tolist()
                flattened_xyn = [point for sublist in xyn_list[0] for point in sublist]

                if xyn.size == 0:
                    continue

                # 예외처리 : id 없는 경우
                if (person.boxes.id == None):
                    continue
                id = int(person.boxes.id.item()) # id 값 받아오기
                # id에 해당하는 저장공간 확보
                while(len(frame_keypoints_xyn) < id):
                    frame_keypoints_xyn.append([])
                # 각 id마다 길이가 10이 넘지 않도록 과거 데이터 삭제 후 현재 데이터 추가
                if len(frame_keypoints_xyn[id-1]) == 10:
                    frame_keypoints_xyn[id-1] = frame_keypoints_xyn[id-1][1:]
                frame_keypoints_xyn[id-1].append(flattened_xyn)

                # id와 keypoints를 id_keypoints 리스트에 저장
                id_keypionts.append(id)
                flattened_xy = [int(point)-1 for sublist in keypoints.xy.cpu().tolist()[0] for point in sublist]
                id_keypionts.extend(flattened_xy)

                # LSTM 모델을 사용하여 예측
                if len(frame_keypoints_xyn[id-1]) > 0:
                    new_data = np.array(frame_keypoints_xyn[id-1])  # 예측하고자 하는 새로운 데이터

                    # 3-D 텐서로 입력 데이터 조정
                    new_data = torch.FloatTensor(new_data)  # (sequence_length, input_size)
                    new_data = new_data.unsqueeze(0)  # (1, sequence_length, input_size)

                    if new_data.size(-1) == input_size: # 입력 데이터의 차원이 맞는지 확인
                        lstm_prediction = loaded_model(new_data)

                        # 예측 결과를 클래스 확률로 변환
                        softmax = torch.nn.Softmax(dim=1)
                        class_probabilities = softmax(lstm_prediction)
                        class_probabilities_numpy = class_probabilities.cpu().detach().numpy()

                        # 예측 결과 출력
                        # print("LSTM 모델 예측 결과 (클래스 확률):", class_probabilities_numpy)
                        if (0): # todo: 실험용 코드이므로 삭제할 것
                        # if (class_probabilities_numpy[0][1] < 0.6).any(): # sit
                            xykey = Int32MultiArray()
                            xykey.data = [0] # 'sit'
                            # print("sit")
                        else: # stand
                            xykey = Int32MultiArray()
                            xykey.data = [(msg.header.stamp.sec % 1000000) * 1000 + msg.header.stamp.nanosec // 1000000] + id_keypionts
                            '''
                            xykey format description
                                color image time stamp (millisecond),
                                id, keypoints.xy,
                                id, keypoints.xy,
                                id, keypoints.xy,
                                ...
                            '''
                            self.seg_publisher.publish(xykey)
                            # print("stand")
                
                # self.calcualte_processtime(now_time) # todo: 실행시간 계산용
                
            except IndexError as e :
                continue
            

def main(args=None):
    rclpy.init(args=args)
    node = DetectLSTMSegNode()
    rclpy.spin(node)
    rclpy.shutdown()
if __name__ == '__main__':
    main()

