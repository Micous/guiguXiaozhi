package com.atguigu.java.ai.langchain4j.tools;

import com.atguigu.java.ai.langchain4j.entity.Appointment;
import com.atguigu.java.ai.langchain4j.service.AppointmentService;
import com.atguigu.java.ai.langchain4j.service.DoctorInfo;
import com.atguigu.java.ai.langchain4j.service.DoctorService;
import com.atguigu.java.ai.langchain4j.service.SlotAvailability;
import com.atguigu.java.ai.langchain4j.service.SlotService;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * 挂号相关工具。
 * <p>
 * 这里刻意使用扁平的字符串参数（而不是把实体对象整个作为入参）：
 * 小参数量的本地模型对嵌套对象 schema 的理解很差，容易漏传字段或者编造字段值，
 * 扁平参数可以让模型准确地把对话中的信息映射到工具入参上。
 * <p>
 * 工具通过 {@link SlotService} / {@link DoctorService} 访问院内数据：
 * 号源查询是 Mock（真实 HIS 接口本地调不通），专家名录是直接查本地向量库的真实检索。
 * 接入真实 HIS 时只需替换 {@code SlotService} 的 Bean，工具层代码不用动。
 */
@Component
public class AppointmentTools {

    @Autowired
    private AppointmentService appointmentService;

    @Autowired
    private SlotService slotService;

    @Autowired
    private DoctorService doctorService;

    @Tool(name = "预约挂号", value = "【提交预约】当用户要求创建预约、提交挂号、或说“帮我预约/帮我挂号/确认预约”时，调用本工具真正创建预约记录。"
            + "入参：患者姓名、身份证号、预约科室、预约日期(格式如2026-11-20)、预约时段(上午或下午)、预约医生(可选)。"
            + "本工具内部会先查询号源，号源充足才会写入预约记录。"
            + "注意：本工具只用于“提交预约”；如果用户只是询问有没有号源而尚未要求提交，请改用“查询是否有号源”。")
    public String bookAppointment(
            @P("患者姓名") String username,
            @P("患者身份证号") String idCard,
            @P("预约科室名称，例如：神经内科") String department,
            @P("预约日期，格式：2026-11-20") String date,
            @P("预约时段，只能是：上午 或 下午") String time,
            @P(value = "预约医生姓名，可选；用户没指定时可以留空", required = false) String doctorName
    ) {
        // 先查号源：没有号源就不写入记录
        SlotAvailability availability = slotService.check(department, date, time, doctorName);
        if (!availability.available()) {
            return availability.message() + " 因此无法为您办理预约。";
        }

        // 用户没指定医生时，用查询到的出诊医生兜底，保证预约记录完整
        String doctor = (doctorName == null || doctorName.isBlank())
                ? (availability.doctors().isEmpty() ? null : availability.doctors().get(0))
                : doctorName;

        Appointment appointment = new Appointment();
        appointment.setUsername(username);
        appointment.setIdCard(idCard);
        appointment.setDepartment(department);
        appointment.setDate(date);
        appointment.setTime(time);
        appointment.setDoctorName(doctor);

        // 防重复预约：相同科室 + 时段已有预约时直接拦截
        Appointment appointmentDB = appointmentService.getOne(appointment);
        if (appointmentDB == null) {
            appointment.setId(null); // 防止大模型幻觉设置了 id
            if (appointmentService.save(appointment)) {
                return "预约成功。预约详情：" + describe(appointment);
            }
            return "预约失败，请稍后重试";
        }
        return "您在" + department + " " + date + " " + time + "已有预约，请勿重复预约";
    }

    @Tool(name = "取消预约挂号", value = "【取消预约】当用户要求取消、退掉、撤销已有的挂号预约时调用本工具。"
            + "入参：患者姓名、身份证号、预约科室、预约日期(格式如2026-11-20)、预约时段(上午或下午)。"
            + "存在对应预约记录则删除并返回取消成功，否则返回未找到可取消的预约。")
    public String cancelAppointment(
            @P("患者姓名") String username,
            @P("患者身份证号") String idCard,
            @P("预约科室名称，例如：神经内科") String department,
            @P("预约日期，格式：2026-11-20") String date,
            @P("预约时段，只能是：上午 或 下午") String time
    ) {
        Appointment query = new Appointment();
        query.setUsername(username);
        query.setIdCard(idCard);
        query.setDepartment(department);
        query.setDate(date);
        query.setTime(time);

        Appointment appointmentDB = appointmentService.getOne(query);
        if (appointmentDB != null) {
            if (appointmentService.removeById(appointmentDB.getId())) {
                return "取消预约成功，已删除：" + describe(appointmentDB);
            }
            return "取消预约失败，请稍后重试";
        }
        return "未找到可取消的预约记录，请核对姓名、身份证号、科室、日期和时段";
    }

    @Tool(name = "查询是否有号源", value = "【仅查询号源】当用户只是询问某个科室某天上午/下午还有没有号、能不能挂号，但尚未要求提交预约时，调用本工具查询。"
            + "入参：科室名称、日期(格式如2026-11-20)、时间(上午或下午)、医生姓名(可选)。"
            + "注意：本工具只查询不预约；一旦用户要求真正提交预约，请改用“预约挂号”。")
    public String queryDepartment(
            @P("科室名称，例如：神经内科") String name,
            @P("日期，格式：2026-11-20") String date,
            @P("时间，可选值：上午、下午") String time,
            @P(value = "医生名称，可选", required = false) String doctorName
    ) {
        System.out.println("查询是否有号源");
        System.out.println("科室名称：" + name);
        System.out.println("日期：" + date);
        System.out.println("时间：" + time);
        System.out.println("医生名称：" + doctorName);

        SlotAvailability availability = slotService.check(name, date, time, doctorName);
        return availability.available()
                ? availability.message()
                : "暂时没有可预约的号源。" + availability.message();
    }

    @Tool(name = "查询科室专家", value = "【查询专家】当用户询问某个科室有哪些专家医生、哪位医生擅长某类疾病时调用本工具。"
            + "入参：科室名称，可选关键词（如“脑血管病”）。返回该科室的出诊专家及擅长方向。")
    public String queryDoctors(
            @P("科室名称，例如：神经内科") String department,
            @P(value = "擅长方向关键词，可选，例如：脑血管病", required = false) String keyword
    ) {
        List<DoctorInfo> doctors = doctorService.listDoctors(department);
        if (doctors.isEmpty()) {
            return "本院暂无【" + department + "】科室的专家信息，建议先做 AI 分导诊。";
        }
        StringBuilder sb = new StringBuilder();
        sb.append(department).append("的出诊专家有：");
        for (DoctorInfo doctor : doctors) {
            if (keyword != null && !keyword.isBlank()
                    && !doctor.expertise().contains(keyword)
                    && !doctor.name().contains(keyword)) {
                continue;
            }
            sb.append("\n").append(doctor.brief()).append("，擅长：").append(doctor.expertise());
        }
        return sb.toString();
    }

    private String describe(Appointment appointment) {
        return "姓名：" + appointment.getUsername()
                + "，身份证号：" + appointment.getIdCard()
                + "，科室：" + appointment.getDepartment()
                + "，日期：" + appointment.getDate()
                + "，时段：" + appointment.getTime()
                + "，医生：" + appointment.getDoctorName();
    }
}
